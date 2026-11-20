"""``GET /api/projects/{id}/preflight``: the check before submitting (§9.11).

``doc`` names the document the build starts from (as for an export); ``pages=1`` renders
the PDF (from the cache when nothing changed) to count its pages.
"""

from __future__ import annotations

import logging

from ..http.request import HTTPError, Request
from ..http.response import Response, json_response
from ..render import preflight
from .projects import document_access, project_access

log = logging.getLogger("docwizard.preflight")


def register(app) -> None:
    r = app.router

    async def check(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "read")
        renderer = app.renderer
        doc_path = None
        if req.query.get("doc"):
            doc, _, _ = await document_access(app, req, req.query["doc"], "read")
            if doc["project_id"] != project["id"]:
                raise HTTPError(400, "bad_request", "That document is in another project.")
            doc_path = doc["path"]
        snap, _versions = await renderer.snapshot(project["id"])
        inputs = renderer.inputs_for(dict(project), doc_path, snap)

        def counts(c):
            rows = c.execute(
                "SELECT d.path,"
                " (SELECT count(*) FROM comments t WHERE t.doc_id = d.id AND t.id = t.thread_id"
                "   AND t.resolved_at IS NULL AND t.orphaned = 0 AND t.suggestion IS NULL) AS comments,"
                " (SELECT count(*) FROM comments t WHERE t.doc_id = d.id AND t.id = t.thread_id"
                "   AND t.resolved_at IS NULL AND t.orphaned = 0 AND t.suggestion IS NOT NULL) AS suggestions,"
                " (SELECT count(*) FROM conflicts k WHERE k.doc_id = d.id AND k.resolved_at IS NULL) AS conflicts"
                " FROM documents d WHERE d.project_id = ? AND d.deleted_at IS NULL", (project["id"],)).fetchall()
            return {row["path"]: dict(row) for row in rows if row["path"] in inputs}

        review = await app.db.read(counts)
        pages, pdf_error = None, None
        if req.query.get("pages") == "1":
            target = "pdf-latex" if inputs and inputs[0].endswith(".tex") else "pdf-typst"
            try:
                result = await renderer.render(dict(project), target, doc_path=doc_path, actor=req.actor_key)
            except HTTPError as exc:
                result, pdf_error = None, exc.message
            except Exception:  # noqa: BLE001
                log.exception("preflight render failed")
                result, pdf_error = None, "The PDF could not be made."
            if result is not None and result.status == "ok":
                try:
                    pages = preflight.pdf_pages((renderer.renders_dir / result.key / result.output).read_bytes())
                except OSError:
                    pages = None
                if pages is None:
                    pdf_error = "The pages could not be counted."
            elif result is not None:
                pdf_error = next((d.get("message") for d in result.diagnostics if d.get("severity") == "error"),
                                 "The PDF could not be made.")
        gettext = lambda message, **values: app.t(req, message, **values)  # noqa: E731
        report = preflight.run(snap.texts, inputs, review=review, pages=pages, gettext=gettext)
        if pdf_error:
            report["pdfError"] = app.t(req, pdf_error)
        return json_response(report)

    r.add("GET", "/api/projects/{id}/preflight", check, guest=True)
