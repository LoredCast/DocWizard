"""Home, source offer and health check."""

from __future__ import annotations

from ..http.request import Request
from ..http.response import Response, json_response


def register(app) -> None:
    async def home(req: Request) -> Response:
        return app.page(req, "home.html")

    async def source(req: Request) -> Response:
        return app.page(req, "source.html", source_url=app.cfg.server.source_url)

    async def healthz(req: Request) -> Response:
        await app.db.fetchone("SELECT 1")
        return json_response({"ok": True, "release": app.cfg.release})

    app.router.add("GET", "/", home)
    app.router.add("GET", "/source", source, auth=False)
    app.router.add("GET", "/healthz", healthz, auth=False, no_session=True, host_any=True)
