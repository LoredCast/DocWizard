"""Exports, jobs, rendered outputs, HTML previews and the citation library."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..citations.library import CitationLibrary
from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, guess_type, json_response, user_content
from ..projects import files
from ..projects import service as svc
from ..render import synctex
from .projects import document_access, project_access

_KEY = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")

DARK_CSS = (b"html{filter:invert(1) hue-rotate(180deg);background:#fff}"
            b"img,video,svg image,picture{filter:invert(1) hue-rotate(180deg)}")


def preview_csp(app_origin: str, *, same_origin: bool = False) -> str:
    if same_origin:
        return ("sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    return ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
            f"base-uri 'none'; form-action 'none'; frame-ancestors {app_origin}")


def register(app) -> None:
    r = app.router
    renderer = app.renderer
    library = CitationLibrary(app)
    app.citations = library

    async def export(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "export")
        body = await req.json_object()
        target = body.get("target")
        doc_path = None
        if isinstance(body.get("doc"), str):
            doc, _, _ = await document_access(app, req, body["doc"], "export")
            doc_path = doc["path"]
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        if not isinstance(target, str):
            raise bad_request("Choose a format.")
        try:
            job = renderer.start_export(dict(project), req.actor_key, target, doc_path, options,
                                        anonymize=body.get("anonymize") is True)
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"job": job.id}, 202)

    async def job_status(req: Request) -> Response:
        job = renderer.exports.get(req.param("id"))
        if job is None or job.user_id != req.actor_key:
            raise not_found("No such job.")
        out = {"id": job.id, "status": job.status, "target": job.target, "diagnostics": [], "url": None}
        if job.result is not None:
            out["diagnostics"] = job.result.diagnostics
            if job.result.status == "ok":
                out["url"] = f"/api/renders/{job.result.key}/{job.result.output}"
        return json_response(out)

    async def rendered_file(req: Request) -> Response:
        key, name = req.param("key"), req.param("file")
        if not _KEY.match(key):
            raise not_found()
        base = renderer.renders_dir / key
        try:
            meta = json.loads((base / "meta.json").read_text())
        except (OSError, ValueError):
            raise not_found("This output is no longer available. Export again.") from None
        # The cache is keyed by content but served only to people with access to the project.
        await project_access(app, req, meta["project_id"], "read")
        path = _safe_child(base, name)
        if path is None or name != meta["output"]:
            raise not_found()
        return user_content(path, name)

    async def preview(req: Request) -> Response:
        token, key, rel = req.param("token"), req.param("key"), req.param("path")
        if not _TOKEN.match(token) or not _KEY.match(key):
            raise not_found()
        project_id = renderer.token_project(token)
        if project_id is None:
            raise not_found("This preview link expired.")
        base = renderer.renders_dir / key
        try:
            meta = json.loads((base / "meta.json").read_text())
        except (OSError, ValueError):
            raise not_found() from None
        if meta.get("project_id") != project_id:
            raise not_found()
        if rel == "_dark.css":
            body, ctype = DARK_CSS, "text/css"
        else:
            path = _safe_child(base, rel)
            if path is None or not path.is_file() or rel == "meta.json":
                # Images and other resources come from the project's assets.
                try:
                    norm = files.normalize(rel)
                except files.PathError:
                    raise not_found() from None
                asset = await app.db.read(lambda c: svc.get_asset(c, project_id, norm))
                if asset is None:
                    raise not_found()
                path = files.blob_path(app.cfg.data_dir, asset["sha256"])
            body = path.read_bytes()
            ctype = guess_type(rel)
            if rel.endswith(".html") and req.query.get("paper") == "dark":
                body = body.replace(b"</head>", b'<link rel="stylesheet" href="_dark.css"></head>', 1)
        if ctype.startswith("text/") or ctype == "image/svg+xml":
            ctype += "; charset=utf-8"
        resp = Response(body, 200, ctype)
        same_origin = app.cfg.preview_origin == app.cfg.app_origin
        resp.set_header("content-security-policy", preview_csp(app.cfg.app_origin, same_origin=same_origin))
        resp.set_header("referrer-policy", "no-referrer")
        resp.set_header("x-content-type-options", "nosniff")
        resp.set_header("cross-origin-resource-policy", "cross-origin")
        resp.set_header("cache-control", "private, max-age=3600")
        return resp

    async def citations(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "read")
        items, etag = await library.items(project["id"])
        if req.headers.get("if-none-match") == etag:
            resp = Response(b"", 304)
        else:
            resp = json_response({"items": items})
        resp.set_header("etag", etag)
        resp.set_header("cache-control", "private, no-cache")
        return resp

    async def source_at(req: Request) -> Response:
        """Where a point in the PDF preview comes from (§6.4): SyncTeX for LaTeX, else the words
        around it, found in the build's source files."""
        project, _ = await project_access(app, req, req.param("id"), "read")
        body = await req.json_object()
        key = body.get("key")
        if not isinstance(key, str) or not _KEY.match(key):
            raise bad_request("Unknown preview.")
        base = renderer.renders_dir / key
        try:
            meta = json.loads((base / "meta.json").read_text())
        except (OSError, ValueError):
            raise not_found("This preview is no longer available.") from None
        if meta.get("project_id") != project["id"]:
            raise not_found("This preview is no longer available.")
        try:
            page, pages = int(body.get("page") or 0), int(body.get("pages") or 0)
            x, y = float(body.get("x") or 0), float(body.get("y") or 0)
            at = int(body.get("at") or 0)
        except (TypeError, ValueError):
            raise bad_request("Give the page and the point.") from None
        words = body.get("words") if isinstance(body.get("words"), list) else []
        words = [str(w)[:64] for w in words[:80]]
        doc_path = None
        if isinstance(body.get("doc"), str):
            doc, _, _ = await document_access(app, req, body["doc"], "read")
            if doc["project_id"] == project["id"]:
                doc_path = doc["path"]
        snap, _ = await renderer.snapshot(project["id"])

        found = None
        index = base / "synctex.json"
        if index.is_file():
            try:
                hit = synctex.lookup(json.loads(index.read_text()), page, x, y)
            except (OSError, ValueError):
                hit = None
            if hit and hit[0] in snap.texts:
                found = (hit[0], hit[1], 0)
        if found is None and words:
            try:
                inputs = renderer.inputs_for(dict(project), doc_path, snap)
            except HTTPError:
                inputs = []
            if inputs and inputs[0].endswith(".tex"):
                # A LaTeX root pulls in other files with \input and \include.
                inputs += sorted(p for p in snap.texts if p.endswith(".tex") and p not in inputs)
            texts = [(p, snap.texts[p]) for p in inputs if p in snap.texts]
            hint = (page - 0.5) / pages if pages > 0 and page > 0 else 0.5
            hit = synctex.match_words(texts, words, at, hint)
            if hit:
                path, offset = hit
                line, col = synctex.line_col(snap.texts[path], offset)
                found = (path, line, col)
        if found is None:
            raise not_found("This spot couldn't be found in the source.")
        path, line, col = found
        doc = await app.db.read(lambda c: svc.get_document_by_path(c, project["id"], path))
        return json_response({"path": path, "line": line, "column": col, "doc": doc["id"] if doc else None})

    r.add("POST", "/api/projects/{id}/source-at", source_at, guest=True)
    r.add("POST", "/api/projects/{id}/export", export, guest=True)
    r.add("GET", "/api/jobs/{id}", job_status, guest=True)
    r.add("GET", "/api/renders/{key}/{file}", rendered_file, guest=True)
    r.add("GET", "/api/projects/{id}/citations", citations, guest=True)
    r.add("GET", "/_preview/{token}/{key}/{path:path}", preview, auth=False, no_session=True,
          preview_host=True, same_origin_preview=True)


def _safe_child(base: Path, rel: str) -> Path | None:
    try:
        norm = files.normalize(rel)
    except files.PathError:
        return None
    path = base / norm
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if base.resolve() not in resolved.parents or path.is_symlink():
        return None
    return path
