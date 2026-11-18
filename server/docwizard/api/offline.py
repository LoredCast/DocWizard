"""Local-first support (§6.5): whole projects kept in the browser, edits sent without the
editor, and a project as a plain folder.

- ``POST /api/projects/{id}/offline`` returns an editor snapshot for every text document
  of the project that changed since the versions the browser already holds, so a
  project can be opened and edited offline without having opened each document first.
- ``POST /api/documents/{id}/merge`` reconciles edits made offline, like the ``merge``
  message on the socket (§8.6), for documents that aren't open in any tab.
- ``GET /api/projects/{id}/archive`` downloads the project's files as a zip.
"""

from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
from urllib.parse import quote

from ..collab.authority import Peer
from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response
from ..projects import files
from ..projects import service as svc
from .projects import project_access

MAX_DOCS = 500
MAX_MERGE_TEXT = 10_000_000


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w .,()-]+", "", name, flags=re.UNICODE).strip(" .") or "project"
    return cleaned[:80]


def register(app) -> None:
    from ..pages.projects import doc_url, editor_snapshot

    async def offline_project(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("id"), "read")
        body = await req.json_object()
        known = body.get("known") or {}
        if not isinstance(known, dict):
            raise bad_request("known must map document ids to versions.")
        docs = await app.db.read(lambda c: c.execute(
            "SELECT * FROM documents WHERE project_id = ? AND deleted_at IS NULL ORDER BY path",
            (project["id"],)).fetchall())
        if len(docs) > MAX_DOCS:
            raise bad_request(f"Projects with more than {MAX_DOCS} documents can't be kept offline.")
        out, unchanged = [], []
        for doc in docs:
            if known.get(doc["id"]) == doc["version"]:
                unchanged.append(doc["id"])
                continue
            snap = await editor_snapshot(app, req, project, role, doc, recent=False)
            out.append({"url": doc_url(project["id"], doc["path"]), "snapshot": snap})
        return json_response({"project": {"id": project["id"], "name": project["name"]}, "docs": out,
                              "unchanged": unchanged, "savedAt": time.time()})

    async def merge(req: Request) -> Response:
        doc = await app.db.read(lambda c: svc.get_document(c, req.param("id")))
        if doc is None:
            raise not_found("This document doesn't exist.")
        project, role = await project_access(app, req, doc["project_id"], "edit")
        body = await req.json_object()
        merge_id, base, text = body.get("mergeId"), body.get("base"), body.get("text")
        if not (isinstance(merge_id, str) and 8 <= len(merge_id) <= 64 and isinstance(base, str)
                and isinstance(text, str)):
            raise bad_request("Invalid merge.")
        if len(base) > MAX_MERGE_TEXT or len(text) > MAX_MERGE_TEXT:
            raise HTTPError(413, "too_large", "This text is too long.")
        user, guest = req.user, req.guest
        peer = Peer(send=lambda msg: None, user_id=user.id if user else None,
                    name=user.display_name if user else guest.label, role=role, client_id="http",
                    guest_id=guest.id if guest else None, project_id=project["id"])
        room = await app.rooms.get(doc["id"])
        reply = await room.call(lambda: room.merge(peer, merge_id, base, text))

        async def current():
            return room.text(), room.version

        now_text, version = await room.call(current)
        return json_response({"mergeId": merge_id, "conflicts": reply["conflicts"], "version": version,
                              "text": now_text})

    async def archive(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "read")
        pid = project["id"]

        def load(c):
            docs = c.execute("SELECT id, path, line_ending FROM documents WHERE project_id = ? AND deleted_at IS NULL",
                             (pid,)).fetchall()
            assets = c.execute("SELECT path, sha256 FROM assets WHERE project_id = ? AND deleted_at IS NULL",
                               (pid,)).fetchall()
            return [dict(d) for d in docs], [dict(a) for a in assets]

        docs, assets = await app.db.read(load)
        entries: list[tuple[str, bytes]] = []
        for d in docs:
            room = await app.rooms.get(d["id"])

            async def text_of(room=room):
                return room.text()

            entries.append((d["path"], files.encode_text(await room.call(text_of), d["line_ending"])))
        for a in assets:
            try:
                data = await asyncio.to_thread(files.get_blob, app.cfg.data_dir, a["sha256"])
            except OSError:
                continue
            entries.append((a["path"], data))
        folder = _safe_name(project["name"])

        def build() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, data in sorted(entries):
                    zf.writestr(f"{folder}/{path}", data)
            return buf.getvalue()

        data = await asyncio.to_thread(build)
        resp = Response(data, 200, "application/zip")
        resp.set_header("content-disposition",
                        f"attachment; filename=\"{folder.encode('ascii', 'ignore').decode() or 'project'}.zip\";"
                        f" filename*=UTF-8''{quote(folder)}.zip")
        resp.set_header("cache-control", "no-store")
        return resp

    r = app.router
    r.add("POST", "/api/projects/{id}/offline", offline_project, guest=True)
    r.add("POST", "/api/documents/{id}/merge", merge, guest=True)
    r.add("GET", "/api/projects/{id}/archive", archive, guest=True)
