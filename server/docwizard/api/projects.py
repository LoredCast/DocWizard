"""JSON API: projects, documents, assets, members, versions and conflicts (§7.5)."""

from __future__ import annotations

import asyncio
import datetime as dt

from ..auth import permissions
from ..citations import lookup
from ..netpolicy import FetchError, PolicyError
from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response, user_content
from ..projects import docx_import, files, fountain, history, starters
from ..projects import service as svc
from ..render import fields, languages


# Guests never manage a project or see who else is in it (§14.2).
GUEST_FORBIDDEN = {"share", "publish", "sync", "manage_members", "delete_project"}


def actor_role(c, req: Request, project_id: str) -> str | None:
    if req.user is not None:
        return svc.role_of(c, req.user.id, project_id)
    if req.guest is not None and req.guest.project_id == project_id:
        return req.guest.role
    return None


async def project_access(app, req: Request, project_id: str, capability: str):
    """Return (project row, role) or raise 404/403 via permissions.require."""

    def work(c):
        project = svc.get_project(c, project_id)
        role = actor_role(c, req, project_id) if project else None
        return project, role

    project, role = await app.db.read(work)
    permissions.require(role, capability, project)
    if req.guest is not None and capability in GUEST_FORBIDDEN:
        raise HTTPError(403, "forbidden", "Guests can't do this.")
    return project, role


def user_id_of(req: Request) -> int | None:
    return req.user.id if req.user is not None else None


def author_of(req: Request) -> str | None:
    """An operation author: the user id, or "g<id>" for a guest."""
    if req.user is not None:
        return str(req.user.id)
    if req.guest is not None:
        return f"g{req.guest.id}"
    return None


async def document_access(app, req: Request, doc_id: str, capability: str):
    doc = await app.db.read(lambda c: svc.get_document(c, doc_id))
    if doc is None:
        raise not_found("This document doesn't exist or you don't have access.")
    project, role = await project_access(app, req, doc["project_id"], capability)
    return doc, project, role


async def create_project_with_starter(app, user, name: str, layout: str, starter: bool = True,
                                     lang: str = "en") -> tuple[str, str | None, str | None]:
    """Create a project and, with *starter*, the files its layout starts with (projects/starters.py).
    Returns the project id and the id and path of the document to open first."""
    today = dt.date.today().isoformat()
    lay = app.layouts.get(layout) if app.layouts.exists(layout) else None
    extra = fields.starter_lines(lay) if lay is not None else []
    plan = starters.for_layout(lay, lang, user.display_name, today, extra) if starter else None

    def work(c):
        pid = svc.create_project(c, user.id, name, layout)
        main = None
        if plan is not None:
            ids = [svc.create_document(c, pid, path, text) for path, text in plan.files]
            main = ids[0]
            if plan.build:
                svc.update_project(c, pid, build_inputs=plan.build)
        return pid, main

    pid, main = await app.db.write(work)
    # Materialize the new files right away.
    for doc in await app.db.fetchall("SELECT id FROM documents WHERE project_id = ?", (pid,)):
        room = await app.rooms.get(doc["id"])
        await room.call(room.materialize)
    return pid, main, plan.main if plan is not None else None


def register(app) -> None:
    r = app.router
    limits = app.cfg.limits

    async def list_projects(req: Request) -> Response:
        rows = await app.db.read(lambda c: svc.list_projects(c, req.user.id))
        return json_response({"projects": rows})

    async def create_project(req: Request) -> Response:
        body = await req.json_object()
        layout = body.get("layout") or app.policy("default_layout")
        if not app.layouts.exists(layout):
            raise bad_request("Unknown layout.")
        try:
            pid, main, main_path = await create_project_with_starter(
                app, req.user, str(body.get("name", "")), layout, bool(body.get("starter", True)), app.lang_of(req))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"id": pid, "mainDocument": main, "mainPath": main_path}, 201)

    async def get_project(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("id"), "read")
        await app.scanner.scan(project["id"])
        tree = await app.db.read(lambda c: svc.tree(c, project["id"]))
        p = dict(project)
        return json_response({"id": p["id"], "name": p["name"], "layout": p["layout_id"], "role": role,
                              "layoutOptions": _json(p["layout_options"]),
                              "buildInputs": _json(p["build_inputs"]), "lang": p.get("lang"), "tree": tree})

    async def patch_project(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("id"), "change_layout")
        body = await req.json_object()
        kwargs = {}
        if "name" in body:
            permissions.require(role, "manage_members")
            kwargs["name"] = str(body["name"])
        if "layout" in body:
            if not app.layouts.exists(str(body["layout"])):
                raise bad_request("Unknown layout.")
            kwargs["layout_id"] = str(body["layout"])
        if "layoutOptions" in body:
            layout = app.layouts.get(kwargs.get("layout_id", project["layout_id"]))
            try:
                kwargs["layout_options"] = layout.validate_options(body["layoutOptions"])
            except ValueError as exc:
                raise bad_request(str(exc)) from None
        if "lang" in body:
            # The document language (§9.8): a language tag, or null for the layout's.
            value = body["lang"]
            tag = languages.normalize(value) if value not in (None, "") else None
            if value not in (None, "") and tag is None:
                raise bad_request("lang must be a language tag such as en-GB or de-AT.")
            kwargs["lang"] = tag
        if "buildInputs" in body:
            inputs = body["buildInputs"]
            if inputs is not None:
                if not isinstance(inputs, list) or not all(isinstance(p, str) for p in inputs) or len(inputs) > 200:
                    raise bad_request("buildInputs must be a list of paths.")
                try:
                    inputs = [files.normalize(p) for p in inputs]
                except files.PathError as exc:
                    raise bad_request(str(exc)) from None
            kwargs["build_inputs"] = inputs or None
        try:
            await app.db.write(lambda c: svc.update_project(c, project["id"], **kwargs))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"ok": True})

    async def delete_project(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "delete_project")
        await app.db.write(lambda c: svc.delete_project(c, project["id"]))
        await app.publisher.prune()
        return json_response({"ok": True})

    async def tree(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "read")
        await app.scanner.scan(project["id"])
        return json_response({"tree": await app.db.read(lambda c: svc.tree(c, project["id"]))})

    async def create_document(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "edit")
        body = await req.json_object()
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > limits.max_document_bytes:
            raise bad_request("Invalid text.")
        try:
            doc_id = await app.db.write(lambda c: svc.create_document(c, project["id"], str(body.get("path", "")),
                                                                      text.replace("\r\n", "\n")))
        except (ValueError, files.PathError) as exc:
            raise bad_request(str(exc)) from None
        room = await app.rooms.get(doc_id)
        await room.call(room.materialize)
        return json_response({"id": doc_id}, 201)

    async def patch_document(req: Request) -> Response:
        doc, project, _ = await document_access(app, req, req.param("id"), "edit")
        body = await req.json_object()
        if "path" in body:
            old_path = doc["path"]
            try:
                new_path = await app.db.write(lambda c: svc.rename_document(c, doc, str(body["path"])))
            except (ValueError, files.PathError) as exc:
                raise bad_request(str(exc)) from None
            if new_path != old_path:
                room = await app.rooms.get(doc["id"])

                async def move():
                    room.path = new_path
                    await room.materialize(force=True)
                    await asyncio.to_thread(files.remove, room.project_dir, old_path)

                await room.call(move)
            return json_response({"path": new_path})
        if "pinned" in body and req.user is not None:
            await app.db.write(lambda c: svc.set_pinned(c, req.user.id, doc["id"], bool(body["pinned"])))
        return json_response({"ok": True})

    async def delete_document(req: Request) -> Response:
        doc, project, _ = await document_access(app, req, req.param("id"), "edit")
        await app.db.write(lambda c: svc.trash_document(c, doc["id"]))
        await app.rooms.close_document(doc["id"])
        await asyncio.to_thread(files.remove, app.rooms.project_dir(project["id"]), doc["path"])
        await app.publisher.prune()
        return json_response({"ok": True})

    async def put_asset(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "upload")
        try:
            path = files.normalize(req.param("path"))
        except files.PathError as exc:
            raise bad_request(str(exc)) from None
        data = await req.body(limits.max_upload_bytes)
        if path.lower().endswith(".docx"):
            return await import_docx(project, path, data, req)
        if path.lower().endswith(".fountain"):
            # Fountain screenplays become Markdown documents for the script layouts (§9.3).
            try:
                text = fountain.to_markdown(files.decode_text(data)[0])
            except UnicodeDecodeError:
                raise bad_request("Text files must be UTF-8.") from None
            path, data = path[: -len(".fountain")] + ".md", text.encode()
        kind = files.kind_for(path)
        if kind is not None:
            # Text files become documents.
            try:
                text, ending = files.decode_text(data)
            except UnicodeDecodeError:
                raise bad_request("Text files must be UTF-8.") from None
            existing = await app.db.read(lambda c: svc.get_document_by_path(c, project["id"], path))
            if existing:
                room = await app.rooms.get(existing["id"])
                await room.call(lambda: room.apply_text(text, "system:upload", author_of(req)))
                return json_response({"id": existing["id"], "type": "document"})
            try:
                doc_id = await app.db.write(lambda c: svc.create_document(c, project["id"], path, text, ending))
            except ValueError as exc:
                raise bad_request(str(exc)) from None
            room = await app.rooms.get(doc_id)
            await room.call(room.materialize)
            return json_response({"id": doc_id, "type": "document"}, 201)
        root = app.rooms.project_dir(project["id"])
        if req.query.get("unique") == "1":
            # Pasted and dropped images never replace a file: figures/figure.png becomes figure-2.png.
            path = await app.db.read(lambda c: svc.free_path(c, project["id"], path))

        def store():
            digest = files.put_blob(app.cfg.data_dir, data)
            st = files.atomic_write(root, path, data)
            return digest, st

        digest, st = await asyncio.to_thread(store)
        try:
            await app.db.write(lambda c: svc.put_asset(c, project["id"], path, digest, len(data), st))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"path": path, "type": "asset", "size": len(data)}, 201)

    async def store_asset(project_id: str, path: str, data: bytes) -> None:
        root = app.rooms.project_dir(project_id)

        def store():
            digest = files.put_blob(app.cfg.data_dir, data)
            st = files.atomic_write(root, path, data)
            return digest, st

        digest, st = await asyncio.to_thread(store)
        await app.db.write(lambda c: svc.put_asset(c, project_id, path, digest, len(data), st))

    async def import_docx(project, path: str, data: bytes, req: Request) -> Response:
        """A Word file becomes a Markdown document; its comments become threads and its tracked
        changes suggestions (§8.11). Images go to a folder next to it."""
        stem = path[: -len(".docx")]
        markdown, media = await app.renderer.convert_docx(data)
        folder = f"{stem}-media"
        markdown = markdown.replace("../out/media/", folder + "/")
        text, threads = docx_import.parse(markdown)

        def free_path(c):
            n = 1
            while True:
                candidate = f"{stem}.md" if n == 1 else f"{stem}-{n}.md"
                if not svc.get_document_by_path(c, project["id"], candidate):
                    return candidate
                n += 1

        doc_path = await app.db.read(free_path)
        try:
            for rel, blob in media.items():
                await store_asset(project["id"], files.normalize(f"{folder}/{rel}"), blob)
            doc_id = await app.db.write(lambda c: svc.create_document(c, project["id"], doc_path, text))
        except (ValueError, files.PathError) as exc:
            raise bad_request(str(exc)) from None
        room = await app.rooms.get(doc_id)
        await room.call(room.materialize)
        made = {"comments": 0, "suggestions": 0}
        for t in threads:
            a = len(text[:t.start].encode("utf-16-le")) // 2
            b = a + len(text[t.start:t.end].encode("utf-16-le")) // 2

            def insert(c, frm, to, v, quote, t=t):
                cid = c.execute(
                    "INSERT INTO comments(doc_id, author_name, body, quote, anchor_from, anchor_to, anchor_version,"
                    " suggestion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, f"{t.author} (Word)", t.body, quote if t.suggestion is not None else quote[:500],
                     frm, to, v, t.suggestion)).lastrowid
                c.execute("UPDATE comments SET thread_id = id WHERE id = ?", (cid,))
                for author, body in t.replies:
                    c.execute("INSERT INTO comments(doc_id, thread_id, author_name, body) VALUES (?, ?, ?, ?)",
                              (doc_id, cid, f"{author} (Word)", body))
                return cid

            await room.call(lambda: room.add_thread(room.version, a, b, insert))
            made["suggestions" if t.suggestion is not None else "comments"] += 1
        return json_response({"id": doc_id, "type": "document", "path": doc_path, "imported": "docx", **made}, 201)

    async def add_reference(req: Request) -> Response:
        """Look up a DOI, ISBN or arXiv ID and add it to a CSL-JSON file of the project (§10.5)."""
        project, _ = await project_access(app, req, req.param("id"), "edit")
        body = await req.json_object()
        target = str(body.get("file") or "references.json")
        try:
            target = files.normalize(target)
        except files.PathError as exc:
            raise bad_request(str(exc)) from None
        if not target.endswith(".json"):
            raise bad_request("References are added to a CSL-JSON file (.json).")
        try:
            item = await asyncio.to_thread(lookup.fetch_item, app.net, str(body.get("id", "")))
        except lookup.IdentifierError as exc:
            raise HTTPError(422, "not_found", str(exc)) from None
        except (FetchError, PolicyError) as exc:
            raise HTTPError(502, "lookup_failed", f"The lookup failed: {exc}") from None
        existing = await app.db.read(lambda c: svc.get_document_by_path(c, project["id"], target))
        if existing:
            room = await app.rooms.get(existing["id"])

            async def append():
                try:
                    text, key, added = lookup.add_to_file(room.text(), item)
                except lookup.IdentifierError as exc:
                    raise HTTPError(409, "bad_file", str(exc)) from None
                if added:
                    await room.apply_text(text, "system:reference", author_of(req))
                return key, added

            key, added = await room.call(append)
        else:
            text, key, added = lookup.add_to_file(None, item)
            try:
                doc_id = await app.db.write(lambda c: svc.create_document(c, project["id"], target, text))
            except ValueError as exc:
                raise bad_request(str(exc)) from None
            room = await app.rooms.get(doc_id)
            await room.call(room.materialize)
        return json_response({"key": key, "file": target, "added": added, "item": {"id": key, **item}},
                             201 if added else 200)

    async def get_asset(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "read")
        try:
            path = files.normalize(req.param("path"))
        except files.PathError:
            raise not_found() from None
        asset = await app.db.read(lambda c: svc.get_asset(c, project["id"], path))
        if asset is None:
            raise not_found("No such file.")
        return user_content(files.blob_path(app.cfg.data_dir, asset["sha256"]), path)

    async def delete_asset(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "upload")
        path = req.param("path")
        await app.db.write(lambda c: svc.trash_asset(c, project["id"], path))
        await asyncio.to_thread(files.remove, app.rooms.project_dir(project["id"]), path)
        return json_response({"ok": True})

    async def members(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "read")
        return json_response({"members": await app.db.read(lambda c: svc.list_members(c, project["id"]))})

    async def add_member(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "manage_members")
        body = await req.json_object()
        try:
            await app.db.write(lambda c: svc.set_member(c, project["id"], str(body.get("username", "")),
                                                        str(body.get("role", ""))))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"ok": True})

    async def remove_member(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "manage_members")
        body = await req.json_object()
        if not isinstance(body.get("userId"), int):
            raise bad_request("userId is required.")
        try:
            await app.db.write(lambda c: svc.remove_member(c, project["id"], body["userId"]))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"ok": True})

    async def list_versions(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "read")
        def load(c):
            return svc.list_versions(c, doc["id"]), history.list_history(c, doc["id"])

        named, automatic = await app.db.read(load)
        return json_response({"versions": named, "history": automatic})

    async def create_version(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "versions")
        body = await req.json_object()
        label = str(body.get("label", "")).strip()[:120]
        if not label:
            raise bad_request("Name the version.")
        room = await app.rooms.get(doc["id"])
        version = await room.call(lambda: room.snapshot(label, user_id_of(req)))
        return json_response({"version": version}, 201)

    async def restore_version(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "versions")
        snap_id = req.param("v")
        row = await app.db.fetchone("SELECT text FROM doc_snapshots WHERE id = ? AND doc_id = ?",
                                    (int(snap_id) if snap_id.isdigit() else -1, doc["id"]))
        if row is None:
            raise not_found("No such version.")
        room = await app.rooms.get(doc["id"])
        user_id = user_id_of(req)

        async def restore():
            # Keep the text being replaced, so a restore can be undone.
            await room.snapshot(f"Before restore, {dt.datetime.now(dt.UTC):%d %b %H:%M}", user_id)
            return await room.apply_text(row["text"], "system:restore", user_id)

        version = await room.call(restore)
        return json_response({"version": version})

    async def version_text(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "read")
        snap_id = req.param("v")
        row = await app.db.fetchone("SELECT text, label, version FROM doc_snapshots WHERE id = ? AND doc_id = ?",
                                    (int(snap_id) if snap_id.isdigit() else -1, doc["id"]))
        if row is None:
            raise not_found("No such version.")
        return json_response(dict(row))

    async def compare_version(req: Request) -> Response:
        """A version compared with the current text, or with another version (?with=<id>) (§8.5)."""
        doc, _, _ = await document_access(app, req, req.param("id"), "read")
        snap_id = req.param("v")
        row = await app.db.fetchone("SELECT text FROM doc_snapshots WHERE id = ? AND doc_id = ?",
                                    (int(snap_id) if snap_id.isdigit() else -1, doc["id"]))
        if row is None:
            raise not_found("No such version.")
        other = req.query.get("with", "current")
        if other == "current":
            room = await app.rooms.get(doc["id"])
            newer = await room.call(lambda: _text(room))
        else:
            o = await app.db.fetchone("SELECT text FROM doc_snapshots WHERE id = ? AND doc_id = ?",
                                      (int(other) if other.isdigit() else -1, doc["id"]))
            if o is None:
                raise not_found("No such version.")
            newer = o["text"]
        return json_response(await asyncio.to_thread(history.compare, row["text"], newer))

    r.add("GET", "/api/projects", list_projects)
    r.add("POST", "/api/projects", create_project)
    r.add("POST", "/api/projects/{id}/references", add_reference)
    r.add("GET", "/api/projects/{id}", get_project, guest=True)
    r.add("PATCH", "/api/projects/{id}", patch_project, guest=True)
    r.add("DELETE", "/api/projects/{id}", delete_project)
    r.add("GET", "/api/projects/{id}/tree", tree, guest=True)
    r.add("POST", "/api/projects/{id}/documents", create_document, guest=True)
    r.add("PUT", "/api/projects/{id}/assets/{path:path}", put_asset, guest=True)
    r.add("GET", "/api/projects/{id}/assets/{path:path}", get_asset, guest=True)
    r.add("DELETE", "/api/projects/{id}/assets/{path:path}", delete_asset, guest=True)
    r.add("GET", "/api/projects/{id}/members", members)
    r.add("POST", "/api/projects/{id}/members", add_member)
    r.add("DELETE", "/api/projects/{id}/members", remove_member)
    r.add("PATCH", "/api/documents/{id}", patch_document, guest=True)
    r.add("DELETE", "/api/documents/{id}", delete_document, guest=True)
    r.add("GET", "/api/documents/{id}/versions", list_versions, guest=True)
    r.add("POST", "/api/documents/{id}/versions", create_version, guest=True)
    r.add("GET", "/api/documents/{id}/versions/{v}", version_text, guest=True)
    r.add("GET", "/api/documents/{id}/versions/{v}/compare", compare_version, guest=True)
    r.add("POST", "/api/documents/{id}/versions/{v}/restore", restore_version, guest=True)


async def _text(room) -> str:
    return room.text()


def _json(value):
    import json

    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
