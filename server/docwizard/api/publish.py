"""JSON API: a document's publication as a static site (§14.4)."""

from __future__ import annotations

import json
import sqlite3

from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response, json_response
from ..render.publish import PublishError, check_slug, unlisted_slug
from .projects import document_access


def _public(app, pub, doc_version: int | None = None) -> dict | None:
    if pub is None:
        return None
    out = {"slug": pub["slug"], "url": app.publisher.url_for(pub["slug"]), "visibility": pub["visibility"],
           "autoUpdate": bool(pub["auto_update"]), "withPdf": bool(pub["with_pdf"]), "status": pub["status"],
           "lastError": pub["last_error"], "version": pub["version"], "updatedAt": pub["updated_at"]}
    if doc_version is not None:
        out["outdated"] = pub["version"] is None or pub["version"] < doc_version
    return out


def register(app) -> None:
    r = app.router

    async def get_pub(doc_id: str):
        return await app.db.fetchone("SELECT * FROM publications WHERE doc_id = ?", (doc_id,))

    async def show(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "publish")
        return json_response({"publication": _public(app, await get_pub(doc["id"]), doc["version"]),
                              "baseUrl": app.cfg.server.publish_url.rstrip("/") + "/"})

    async def put(req: Request) -> Response:
        """Create or change the publication, then publish it."""
        doc, project, _ = await document_access(app, req, req.param("id"), "publish")
        app.require_verified_email(req, "publish")
        body = await req.json_object()
        visibility = body.get("visibility", "public")
        if visibility not in ("public", "unlisted"):
            raise bad_request("Choose public or unlisted.")
        current = await get_pub(doc["id"])
        if visibility == "unlisted":
            keep = current is not None and current["visibility"] == "unlisted"
            slug = current["slug"] if keep else unlisted_slug()
        else:
            try:
                slug = check_slug(str(body.get("slug") or ""))
            except PublishError as exc:
                raise bad_request(str(exc)) from None
        auto, with_pdf = int(bool(body.get("autoUpdate"))), int(bool(body.get("withPdf")))

        def work(c):
            if current is None:
                pid = c.execute(
                    "INSERT INTO publications(project_id, doc_id, slug, visibility, auto_update, with_pdf, created_by)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project["id"], doc["id"], slug, visibility, auto, with_pdf, req.user.id)).lastrowid
            else:
                pid = current["id"]
                c.execute("UPDATE publications SET slug = ?, visibility = ?, auto_update = ?, with_pdf = ?"
                          " WHERE id = ?", (slug, visibility, auto, with_pdf, pid))
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'publish', ?, ?)",
                      (f"user:{req.user.id}", doc["id"], json.dumps({"slug": slug, "visibility": visibility})))
            return pid

        try:
            pub_id = await app.db.write(work)
        except sqlite3.IntegrityError:
            raise HTTPError(409, "slug_taken", "This address is already taken. Choose another.") from None
        if current is not None and current["slug"] != slug:
            app.publisher.remove_site(current["slug"])
        return await run(req, doc, pub_id)

    async def run(req: Request, doc, pub_id: int) -> Response:
        try:
            await app.publisher.publish(pub_id)
        except PublishError:
            pass  # the publication row carries the error
        pub = await app.db.fetchone("SELECT * FROM publications WHERE id = ?", (pub_id,))
        refreshed = await app.db.fetchone("SELECT version FROM documents WHERE id = ?", (doc["id"],))
        return json_response({"publication": _public(app, pub, refreshed["version"] if refreshed else None)})

    async def republish(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "publish")
        app.require_verified_email(req, "publish")
        pub = await get_pub(doc["id"])
        if pub is None:
            raise bad_request("This document isn't published.")
        return await run(req, doc, pub["id"])

    async def delete(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "publish")
        pub = await get_pub(doc["id"])
        if pub is not None:
            await app.publisher.unpublish(pub["id"])
            await app.db.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'unpublish', ?, ?)",
                                 (f"user:{req.user.id}", doc["id"], json.dumps({"slug": pub["slug"]})))
        return json_response({"publication": None})

    r.add("GET", "/api/documents/{id}/publication", show)
    r.add("PUT", "/api/documents/{id}/publication", put)
    r.add("POST", "/api/documents/{id}/publication/run", republish)
    r.add("DELETE", "/api/documents/{id}/publication", delete)
