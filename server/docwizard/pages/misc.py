"""Home, source offer, health and readiness checks."""

from __future__ import annotations

import asyncio
import shutil
import time

from ..http.request import Request
from ..http.response import Response, json_response


READY_MIN_FREE_BYTES = 200_000_000


def register(app) -> None:
    async def source(req: Request) -> Response:
        return app.page(req, "source.html", source_url=app.cfg.server.source_url)

    async def healthz(req: Request) -> Response:
        await app.db.fetchone("SELECT 1")
        return json_response({"ok": True, "release": app.cfg.release})

    async def readyz(req: Request) -> Response:
        """Readiness for a load balancer or orchestrator (§16): the database takes writes, the render
        worker answers and the disks have room. /healthz stays a cheap liveness check."""
        checks: dict[str, str] = {}
        try:
            await asyncio.wait_for(app.db.execute(
                "INSERT OR REPLACE INTO health_probe(id, checked_at) VALUES (1, ?)", (time.time(),)), 5)
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {exc.__class__.__name__}"
        try:
            await asyncio.wait_for(app.renderer.worker.hello(), 5)
            checks["worker"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["worker"] = f"error: {exc.__class__.__name__}"

        def disk() -> str:
            for path in (app.cfg.data_dir, app.cfg.jobs_dir, app.cfg.cache_dir, app.publisher.root):
                existing = path.absolute()
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent  # made when first needed
                try:
                    usage = shutil.disk_usage(existing)
                except OSError:
                    return f"error: {path.name} unreadable"
                if usage.free < READY_MIN_FREE_BYTES:
                    return f"error: {path.name} almost full"
            return "ok"

        checks["disk"] = await asyncio.to_thread(disk)
        ok = all(v == "ok" for v in checks.values())
        return json_response({"ok": ok, "checks": checks, "release": app.cfg.release}, 200 if ok else 503)

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
    app.router.add("GET", "/readyz", readyz, auth=False, no_session=True, host_any=True)
