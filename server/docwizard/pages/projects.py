"""Home, project and editor pages."""

from __future__ import annotations

import json
import secrets
import time
from urllib.parse import quote

from ..api.projects import create_project_with_starter, project_access
from ..auth import permissions
from ..http.request import HTTPError, Request, not_found
from ..http.response import Response, redirect
from ..http.templates import script_json
from ..projects import files
from ..projects import service as svc


def doc_url(project_id: str, path: str) -> str:
    return f"/p/{project_id}/{quote(path)}"


def register(app) -> None:
    async def home(req: Request) -> Response:
        uid = req.user.id

        def load(c):
            return svc.list_recent(c, uid), svc.list_projects(c, uid)

        recent, projects = await app.db.read(load)
        return app.page(req, "home.html", recent=recent, projects=projects, layouts=app.layouts.list(),
                        default_layout=app.cfg.render.default_layout, doc_url=doc_url, error=req.query.get("error"))

    async def new_project(req: Request) -> Response:
        form = await req.form()
        layout = form.get("layout") or app.cfg.render.default_layout
        if not app.layouts.exists(layout):
            layout = app.cfg.render.default_layout
        try:
            pid, main = await create_project_with_starter(app, req.user, form.get("name", ""), layout,
                                                          form.get("starter", "1") == "1")
        except ValueError as exc:
            return redirect("/?error=" + quote(str(exc)))
        if main:
            return redirect(doc_url(pid, "paper.md"))
        return redirect(f"/p/{pid}")

    async def project_page(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("project"), "read")
        await app.scanner.scan(project["id"])

        def load(c):
            return svc.tree(c, project["id"]), svc.list_members(c, project["id"])

        tree, members = await app.db.read(load)
        return app.page(req, "project.html", project=project, role=role, tree=tree, members=members,
                        layouts=app.layouts.list(), doc_url=doc_url, can=permissions.can,
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
            elif action == "rename":
                await project_access(app, req, pid, "manage_members")
                await app.db.write(lambda c: svc.update_project(c, pid, name=form.get("name", "")))
            elif action == "add-member":
                await project_access(app, req, pid, "manage_members")
                await app.db.write(lambda c: svc.set_member(c, pid, form.get("username", "").strip(),
                                                            form.get("role", "")))
            elif action == "delete":
                await project_access(app, req, pid, "delete_project")
                await app.db.write(lambda c: svc.delete_project(c, pid))
                return redirect("/")
            else:
                raise HTTPError(400, "bad_request", "Unknown action.")
        except (ValueError, files.PathError) as exc:
            return redirect(back + "?error=" + quote(str(exc)))
        return redirect(back)

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
        client_id = "c_" + secrets.token_hex(16)
        user = req.user
        now = time.time()

        def work(c):
            c.execute("INSERT INTO collab_clients(client_id, doc_id, user_id, created_at, last_seen_at)"
                      " VALUES (?, ?, ?, ?, ?)", (client_id, doc["id"], user.id, now, now))
            svc.touch_recent(c, user.id, doc["id"])

        await app.db.write(work)
        room = await app.rooms.get(doc["id"])
        text, version = await room.call(lambda: _text_version(room))
        tree = await app.db.read(lambda c: svc.tree(c, pid))
        layout = app.layouts.get(project["layout_id"]) if app.layouts.exists(project["layout_id"]) \
            else app.layouts.get(app.cfg.render.default_layout)
        try:
            options = json.loads(project["layout_options"] or "{}")
        except ValueError:
            options = {}
        snapshot = {
            "doc": {"id": doc["id"], "path": doc["path"], "kind": doc["kind"]},
            "project": {"id": pid, "name": project["name"]},
            "version": version,
            "text": text,
            "clientID": client_id,
            "role": role,
            "user": {"id": user.id, "name": user.display_name},
            "layout": layout.public(),
            "layoutOptions": options,
            "files": [{"path": e["path"], "type": e["type"]} for e in tree],
            "prefs": {"font": user.preferences.get("editor_font", "sans"),
                      "size": user.preferences.get("editor_size", 17)},
            "previewOrigin": app.cfg.preview_origin,
        }
        return app.page(req, "editor.html", project=project, doc=doc, role=role, snapshot=script_json(snapshot),
                        layout=layout, can=permissions.can)

    app.router.add("GET", "/", home)
    app.router.add("POST", "/projects", new_project)
    app.router.add("GET", "/p/{project}", project_page)
    app.router.add("POST", "/p/{project}", project_form)
    app.router.add("GET", "/p/{project}/{path:path}", editor)


async def _text_version(room):
    return room.text(), room.version
