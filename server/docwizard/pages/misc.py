"""Home, source offer and health check."""

from __future__ import annotations

from ..http.request import Request
from ..http.response import Response, json_response


def register(app) -> None:
    async def source(req: Request) -> Response:
        return app.page(req, "source.html", source_url=app.cfg.server.source_url)

    async def healthz(req: Request) -> Response:
        await app.db.fetchone("SELECT 1")
        return json_response({"ok": True, "release": app.cfg.release})

    async def search_page(req: Request) -> Response:
        q = req.query.get("q", "").strip()[:200]
        results = await app.search.query(req.user.id, q) if q else []
        from .projects import doc_url

        return app.page(req, "search.html", q=q, results=results, available=app.search.available, doc_url=doc_url)

    async def search_api(req: Request) -> Response:
        q = req.query.get("q", "").strip()[:200]
        return json_response({"results": await app.search.query(req.user.id, q) if q else [],
                              "available": app.search.available})

    async def notifications_page(req: Request) -> Response:
        from .. import notifications
        from .projects import doc_url

        uid = req.user.id
        if req.method == "POST":
            await app.db.write(lambda c: notifications.mark_read(c, uid))
            from ..http.response import redirect

            return redirect("/notifications")

        def load(c):
            return notifications.list_for(c, uid, limit=200), notifications.unread_count(c, uid)

        items, unread = await app.db.read(load)
        return app.page(req, "notifications.html", items=items, unread=unread, doc_url=doc_url,
                        mail=app.mail.enabled)

    app.router.add("GET", "/notifications", notifications_page)
    app.router.add("POST", "/notifications", notifications_page)
    app.router.add("GET", "/search", search_page)
    app.router.add("GET", "/api/search", search_api)
    app.router.add("GET", "/source", source, auth=False)
    app.router.add("GET", "/healthz", healthz, auth=False, no_session=True, host_any=True)
