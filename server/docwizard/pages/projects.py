"""Home, project and editor pages."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from urllib.parse import quote, urlencode

from ..api import browser_render, grammar
from ..api.projects import (GUEST_FORBIDDEN, create_project_with_starter, empty_trash, enforce_quota, project_access,
                            rename_file, restore_file, trash_file)
from ..auth import passwords, permissions
from ..http.request import HTTPError, Request, not_found
from ..http.response import Response, redirect
from ..http.templates import script_json
from ..projects import files
from ..projects import service as svc
from ..render import fields, languages
from ..sharing import links


def doc_url(project_id: str, path: str) -> str:
    return f"/p/{project_id}/{quote(path)}"


# -- Organizing the home page (§6.2) ------------------------------------------
# The filter bar works without JavaScript as a GET form; with it, web/app/home.js applies the same
# rules as people type, and keeps them in the address.

SORTS = ("name", "-name", "layout", "-layout", "updated", "-updated", "members", "-members")


def home_filter(query: dict, layout_names: dict) -> dict:
    sort = query.get("sort", "name")
    return {"q": query.get("q", "")[:200], "tag": query.get("tag", "")[:64],
            "layout": query.get("layout", "")[:120], "shared": query.get("shared") == "1",
            "archived": query.get("archived") == "1", "sort": sort if sort in SORTS else "name"}


def project_matches(p: dict, flt: dict, layout_names: dict, extra: str = "") -> bool:
    """Whether a project (or a recent document of it, *extra* being its path) passes the filter."""
    if p["archived"] != flt["archived"]:
        return False
    if flt["tag"] and flt["tag"].casefold() not in {t.casefold() for t in p["tags"]}:
        return False
    if flt["layout"] and p["layout_id"] != flt["layout"]:
        return False
    if flt["shared"] and not p["shared"]:
        return False
    if flt["q"]:
        hay = " ".join([p["name"], *p["tags"], layout_names.get(p["layout_id"], p["layout_id"]),
                        p.get("owner_name") or "", extra]).casefold()
        return all(word in hay for word in flt["q"].casefold().split())
    return True


def _next_sort(current: str, key: str) -> str:
    """A click on a column's header: by that column, or the other way round if it already is."""
    return f"-{key}" if current == key else key


def filter_url(flt: dict, **changes) -> str:
    values = {**flt, **changes}
    params = {"q": values["q"], "tag": values["tag"], "layout": values["layout"],
              "shared": "1" if values["shared"] else "", "archived": "1" if values["archived"] else "",
              "sort": "" if values["sort"] == "name" else values["sort"]}
    query = urlencode({k: v for k, v in params.items() if v})
    return f"/?{query}" if query else "/"


def sort_projects(projects: list[dict], sort: str, layout_names: dict) -> list[dict]:
    key = sort.lstrip("-")
    name = lambda p: p["name"].casefold()  # noqa: E731
    keys = {"name": lambda p: (name(p),),
            "layout": lambda p: (layout_names.get(p["layout_id"], p["layout_id"]).casefold(), name(p)),
            "updated": lambda p: (str(p["updated_at"] or ""), name(p)),
            "members": lambda p: (p["members"], name(p))}
    return sorted(projects, key=keys[key], reverse=sort.startswith("-"))


def _link_roles(app, role: str | None) -> list[str]:
    """Roles a share link may grant: up to the creator's own role and the instance cap (§14.1)."""
    cap = app.policy("guest_max_role")
    return [r for r in links.LINK_ROLES
            if permissions.rank(r) <= permissions.rank(role) and permissions.rank(r) <= permissions.rank(cap)]


async def create_share_link(app, req: Request, project, role: str, values: dict) -> str:
    """Create a link from form or JSON values; returns its URL. Raises ValueError."""
    link_role = str(values.get("role", "viewer"))
    if link_role not in _link_roles(app, role):
        raise ValueError("You can't create a link with this role.")
    days = str(values.get("expires_days", "") or "")
    expires_at = time.time() + int(days) * 86400 if days.isdigit() and 0 < int(days) <= 365 else None
    password = str(values.get("password", "") or "")
    pw_hash = None
    if password:
        if len(password) < 6:
            raise ValueError("Use at least 6 characters for a link password.")
        pw_hash = await asyncio.to_thread(passwords.hash_password, password)
    uid = req.user.id

    def work(c):
        link_id, token = links.create(c, project["id"], link_role, uid, expires_at=expires_at,
                                      password_hash=pw_hash, label=str(values.get("label", "") or ""),
                                      review=values.get("review") in (True, "1", "on"))
        c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'share_link.create', ?, ?)",
                  (f"user:{uid}", project["id"], json.dumps({"link": link_id, "role": link_role})))
        return token

    token = await app.db.write(work)
    return f"{app.cfg.server.public_url.rstrip('/')}/s/{token}"


def register(app) -> None:
    async def home(req: Request) -> Response:
        uid = req.user.id

        from .. import notifications

        def load(c):
            return (svc.list_recent(c, uid), svc.list_projects(c, uid),
                    notifications.list_for(c, uid, limit=10, unread_only=True), notifications.unread_count(c, uid))

        recent, projects, inbox, unread = await app.db.read(load)
        storage = await app.quotas.usage(uid)
        # Mirrored into IndexedDB so the offline home page can list them (§6.5).
        mirror = [{"docId": d["id"], "url": doc_url(d["project_id"], d["path"]), "path": d["path"],
                   "project": d["project"], "openedAt": d["opened_at"], "pinned": bool(d["pinned"])} for d in recent]
        # Organizing (§6.2): the filter bar's choices, and each recent document's project for filtering.
        by_id = {p["id"]: p for p in projects}
        tags = sorted({t for p in projects for t in p["tags"]}, key=str.casefold)
        layout_names = {lay.id: lay.name for lay in app.layouts.visible(uid)}
        used_layouts = sorted({p["layout_id"] for p in projects}, key=lambda i: layout_names.get(i, i).casefold())
        flt = home_filter(req.query, layout_names)
        projects = sort_projects(projects, flt["sort"], layout_names)
        for p in projects:
            p["hidden"] = not project_matches(p, flt, layout_names)
        for d in recent:
            p = by_id.get(d["project_id"])
            d["hidden"] = p is None or not project_matches(p, flt, layout_names, extra=d["path"])
        show_archived = flt["archived"]
        return app.page(req, "home.html", recent=recent, projects=projects, layouts=app.layouts.visible(uid),
                        by_id=by_id, all_tags=tags, used_layouts=[(i, layout_names.get(i, i)) for i in used_layouts],
                        layout_names=layout_names, show_archived=show_archived, flt=flt,
                        sort_url=lambda key: filter_url(flt, sort=_next_sort(flt["sort"], key)),
                        shown_projects=sum(1 for p in projects if not p["hidden"]),
                        shown_recent=sum(1 for d in recent if not d["hidden"]),
                        archived_count=sum(1 for p in projects if p["archived"]),
                        shared_count=sum(1 for p in projects if p["shared"]),
                        inbox=inbox, unread=unread, storage=storage, verify=req.query.get("verify"),
                        email_reminder=bool(app.policy("require_email_verification") and not req.user.is_admin
                                            and not req.user.email_verified),
                        recent_json=script_json(mirror),
                        default_layout=app.policy("default_layout"), doc_url=doc_url, error=req.query.get("error"))

    async def new_project(req: Request) -> Response:
        form = await req.form()
        layout = form.get("layout") or app.policy("default_layout")
        if not app.layouts.exists(layout):
            layout = app.policy("default_layout")
        try:
            pid, main, main_path = await create_project_with_starter(
                app, req.user, form.get("name", ""), layout, form.get("starter", "1") == "1", app.lang_of(req))
        except ValueError as exc:
            return redirect("/?error=" + quote(str(exc)))
        except HTTPError as exc:
            if exc.status != 413:
                raise
            return redirect("/?error=" + quote(exc.message))
        if main:
            return redirect(doc_url(pid, main_path))
        return redirect(f"/p/{pid}")

    async def project_page(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("project"), "read")
        await app.scanner.scan(project["id"])

        guest = req.guest is not None
        may_share = permissions.can(role, "share", project) and not guest

        may_edit = permissions.can(role, "edit", project)

        def load(c):
            return (svc.tree(c, project["id"]), [] if guest else svc.list_members(c, project["id"]),
                    links.list_for_project(c, project["id"]) if may_share else [],
                    svc.list_trash(c, project["id"]) if may_edit and not guest else [])

        tree, members, shares, trash = await app.db.read(load)
        home_state = None
        if not guest and req.user is not None:
            mine = await app.db.fetchone("SELECT tags, archived_at FROM project_members WHERE project_id = ? AND user_id = ?",
                                         (project["id"], req.user.id))
            if mine is not None:
                home_state = {"tags": svc.tags_of(mine["tags"]), "archived": mine["archived_at"] is not None}
        try:
            build_inputs = json.loads(project["build_inputs"]) if project["build_inputs"] else []
        except ValueError:
            build_inputs = []
        may_sync = permissions.can(role, "sync", project) and not guest
        sync = sync_options = None
        if may_sync:
            from ..api.sync import _public as public_target
            from ..api.sync import options as sync_kinds

            sync = public_target(await app.db.fetchone("SELECT * FROM sync_targets WHERE project_id = ?",
                                                       (project["id"],)))
            sync_options = sync_kinds(app)
        return app.page(req, "project.html", project=project, role=role, tree=tree, members=members, trash=trash,
                        home_state=home_state,
                        build_inputs=build_inputs if isinstance(build_inputs, list) else [build_inputs],
                        may_sync=may_sync, sync=sync, sync_options=sync_options,
                        shares=shares, may_share=may_share, link_roles=_link_roles(app, role),
                        new_link=req.query.get("link"), layouts=_layout_choices(app, req, project), doc_url=doc_url,
                        languages=languages.choices(),
                        full_languages={t for t, _ in languages.choices() if languages.full_support(t)},
                        can=lambda r, cap: permissions.can(r, cap, project) and not (guest and cap in GUEST_FORBIDDEN),
                        error=req.query.get("error"))

    async def project_form(req: Request) -> Response:
        pid = req.param("project")
        form = await req.form()
        action = form.get("action")
        back = f"/p/{pid}"
        try:
            if action == "new-document":
                project, _ = await project_access(app, req, pid, "edit")
                path = form.get("path", "").strip()
                if path and "." not in path.rsplit("/", 1)[-1]:
                    path += ".md"
                await enforce_quota(app, pid, 0, new_files=1)
                doc_id = await app.db.write(lambda c: svc.create_document(c, pid, path, ""))
                room = await app.rooms.get(doc_id)
                await room.call(room.materialize)
                return redirect(doc_url(pid, files.normalize(path)))
            if action == "layout":
                await project_access(app, req, pid, "change_layout")
                layout = form.get("layout", "")
                if not app.layouts.exists(layout):
                    raise ValueError("Unknown layout.")
                await app.db.write(lambda c: svc.update_project(c, pid, layout_id=layout))
            elif action == "language":
                await project_access(app, req, pid, "change_layout")
                value = form.get("lang", "")
                tag = languages.normalize(value) if value else None
                if value and tag is None:
                    raise ValueError("Choose a language from the list.")
                await app.db.write(lambda c: svc.update_project(c, pid, lang=tag))
            elif action == "organize":
                # The member's own tags and archive state (§6.2); guests have no home page.
                await project_access(app, req, pid, "read")
                if req.guest is not None:
                    raise HTTPError(403, "forbidden", "Guests have no home page to organize.")
                tags = svc.normalize_tags(form.get("tags", "")) if "tags" in form else None
                archived = {"1": True, "0": False}.get(form.get("archived", ""))
                uid = req.user.id
                await app.db.write(lambda c: svc.set_home_state(c, pid, uid, tags=tags, archived=archived))
                if form.get("next") == "/":
                    return redirect("/")
            elif action == "rename":
                await project_access(app, req, pid, "manage_members")
                await app.db.write(lambda c: svc.update_project(c, pid, name=form.get("name", "")))
            elif action == "add-member":
                await project_access(app, req, pid, "manage_members")
                await app.db.write(lambda c: svc.set_member(c, pid, form.get("username", "").strip(),
                                                            form.get("role", "")))
            elif action == "sharing":
                await project_access(app, req, pid, "manage_members")
                await app.db.write(lambda c: c.execute(
                    "UPDATE projects SET editors_can_share = ?, editors_can_publish = ? WHERE id = ?",
                    (int(form.get("editors_can_share") == "1"), int(form.get("editors_can_publish") == "1"), pid)))
            elif action == "remove-member":
                await project_access(app, req, pid, "manage_members")
                uid = form.get("user_id", "")
                await app.db.write(lambda c: svc.remove_member(c, pid, int(uid) if uid.isdigit() else -1))
            elif action == "create-link":
                project, role = await project_access(app, req, pid, "share")
                url = await create_share_link(app, req, project, role, form)
                return redirect(back + "?link=" + quote(url) + "#share")
            elif action == "revoke-link":
                await project_access(app, req, pid, "share")
                lid = form.get("link_id", "")
                await app.db.write(lambda c: links.revoke(c, pid, int(lid) if lid.isdigit() else -1))
                return redirect(back + "#share")
            elif action == "rename-file":
                await project_access(app, req, pid, "edit")
                new = form.get("new_path", "").strip()
                if new and "." not in new.rsplit("/", 1)[-1] and "." in form.get("path", "").rsplit("/", 1)[-1]:
                    new += "." + form["path"].rsplit(".", 1)[1]
                await rename_file(app, pid, form.get("path", ""), new)
            elif action == "delete-file":
                await project_access(app, req, pid, "edit")
                await trash_file(app, pid, form.get("path", ""))
            elif action == "restore-file":
                await project_access(app, req, pid, "edit")
                kind = "document" if form.get("doc_id") else "asset"
                await restore_file(app, pid, kind, form.get("doc_id") or form.get("path", ""),
                                   form.get("deleted_at", ""))
                return redirect(back + "#trash")
            elif action == "empty-trash":
                project, _ = await project_access(app, req, pid, "delete_project")
                result = await empty_trash(app, pid, project["owner_id"])
                await app.db.execute("INSERT INTO audit_log(actor, action, target, meta_json)"
                                     " VALUES (?, 'trash.empty', ?, ?)", (f"user:{req.user.id}", pid, json.dumps(result)))
                return redirect(back + "#trash")
            elif action == "build-inputs":
                await project_access(app, req, pid, "change_layout")
                inputs = [files.normalize(line.strip()) for line in form.get("inputs", "").splitlines() if line.strip()]
                if len(inputs) > 200:
                    raise ValueError("Use at most 200 build inputs.")
                await app.db.write(lambda c: svc.update_project(c, pid, build_inputs=inputs or None))
                return redirect(back + "#build")
            elif action == "delete":
                await project_access(app, req, pid, "delete_project")
                await app.db.write(lambda c: svc.delete_project(c, pid))
                return redirect("/")
            else:
                raise HTTPError(400, "bad_request", "Unknown action.")
        except (ValueError, files.PathError) as exc:
            return redirect(back + "?error=" + quote(str(exc)))
        except HTTPError as exc:
            if exc.status != 413:
                raise
            return redirect(back + "?error=" + quote(exc.message))
        return redirect(back)

    async def pin(req: Request) -> Response:
        doc_id = req.param("doc")
        form = await req.form()
        await app.db.write(lambda c: svc.set_pinned(c, req.user.id, doc_id, form.get("pinned") == "1"))
        return redirect("/")

    async def editor(req: Request) -> Response:
        pid = req.param("project")
        project, role = await project_access(app, req, pid, "read")
        path = req.param("path")
        doc = await app.db.read(lambda c: svc.get_document_by_path(c, pid, path))
        if doc is None:
            asset = await app.db.read(lambda c: svc.get_asset(c, pid, path))
            if asset is not None:
                return redirect(f"/api/projects/{pid}/assets/{quote(path)}")
            raise not_found("This document doesn't exist.")
        # The review view (§14.1): the text as it reads, with comments and suggestions. Review links
        # open it; anyone can switch with ?view=review or back with ?view=source.
        view = req.query.get("view")
        guest = req.guest
        review = doc["kind"] == "markdown" and (view == "review" or (view != "source" and guest is not None
                                                                      and guest.review))
        snapshot = await editor_snapshot(app, req, project, role, doc, review=review)
        layout = app.layouts.get(snapshot["layout"]["id"])
        return app.page(req, "editor.html", project=project, doc=doc, role=role, snapshot=script_json(snapshot),
                        layout=layout, can=permissions.can, review=review)

    app.router.add("GET", "/", home)
    app.router.add("POST", "/projects", new_project)
    app.router.add("GET", "/p/{project}", project_page, guest=True)
    app.router.add("POST", "/p/{project}", project_form, guest=True)
    app.router.add("POST", "/d/{doc}/pin", pin)
    app.router.add("GET", "/p/{project}/{path:path}", editor, guest=True)


async def editor_snapshot(app, req: Request, project, role: str, doc, *, review: bool = False,
                          recent: bool = True) -> dict:
    """What the editor needs to open a document: its text and version, a new client ID, the
    layout and the person's settings. Embedded in the editor page, and kept in the browser for
    projects that are available offline (§6.5)."""
    pid = project["id"]
    client_id = "c_" + secrets.token_hex(16)
    user, guest = req.user, req.guest
    now = time.time()

    def work(c):
        c.execute("INSERT INTO collab_clients(client_id, doc_id, user_id, guest_id, created_at, last_seen_at)"
                  " VALUES (?, ?, ?, ?, ?, ?)", (client_id, doc["id"], user.id if user else None,
                                                 guest.id if guest else None, now, now))
        if user is not None:
            if recent:
                svc.touch_recent(c, user.id, doc["id"])
            row = c.execute("SELECT pinned FROM recent WHERE user_id = ? AND doc_id = ?",
                            (user.id, doc["id"])).fetchone()
            return bool(row and row["pinned"])
        return False

    pinned = await app.db.write(work)
    room = await app.rooms.get(doc["id"])
    text, version = await room.call(lambda: _text_version(room))
    tree = await app.db.read(lambda c: svc.tree(c, pid))
    # People who can be mentioned in comments (§8.9); guests don't get the member list.
    mentionable = [] if guest is not None else [
        {"username": m["username"], "name": m["display_name"]}
        for m in await app.db.read(lambda c: svc.list_members(c, pid)) if m["id"] != user.id]
    layout = app.layouts.get(project["layout_id"]) if app.layouts.exists(project["layout_id"]) \
        else app.layouts.get(app.policy("default_layout"))
    try:
        options = json.loads(project["layout_options"] or "{}")
    except ValueError:
        options = {}
    snapshot = {
        "doc": {"id": doc["id"], "path": doc["path"], "kind": doc["kind"], "pinned": pinned},
        "project": {"id": pid, "name": project["name"], "lang": project["lang"]},
        "version": version,
        "text": text,
        "clientID": client_id,
        "role": role,
        "user": {"id": req.actor_key, "name": req.actor_name, "guest": guest is not None},
        "can": {cap: permissions.can(role, cap, project) and not (guest and cap in GUEST_FORBIDDEN)
                for cap in ("edit", "comment", "resolve_comments", "versions", "share", "publish",
                            "change_layout", "upload")},
        "layout": layout.public(),
        "layoutOptions": options,
        "fields": fields.for_layout(layout),
        "files": [{"path": e["path"], "type": e["type"]} for e in tree],
        "members": mentionable,
        "prefs": {"font": user.preferences.get("editor_font", "sans") if user else "sans",
                  "size": user.preferences.get("editor_size", 17) if user else 17},
        "previewOrigin": app.cfg.preview_origin,
        "review": review,
        # Grammar checking (§6.8): what the server offers and what the person chose.
        "grammar": {**grammar.available(app), "choice": grammar.choice_of(user.preferences if user else None)},
        # Previews made in this browser with pandoc and Typst as WebAssembly (§6.4).
        "browserRender": browser_render.installed() and doc["kind"] == "markdown",
        # The writing helper (§8.10), for people who turned it on.
        "assist": _assist_on(app, user)
                  and doc["kind"] == "markdown" and permissions.can(role, "comment", project),
    }
    return snapshot


def _assist_on(app, user) -> bool:
    from ..api import assist  # not at the top: api.assist imports api.comments, which imports this module

    return assist.enabled(app) and assist.opted_in(user)


async def _text_version(room):
    return room.text(), room.version


def _layout_choices(app, req, project) -> list:
    """Layouts the project can switch to: the ones this person may use, plus the current one."""
    layouts = app.layouts.visible(req.user.id if req.user else None)
    if all(lay.id != project["layout_id"] for lay in layouts) and app.layouts.exists(project["layout_id"]):
        layouts.append(app.layouts.get(project["layout_id"]))
    return layouts
