"""ASGI entry point: dispatches http, websocket and lifespan scopes (§7.2)."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path

from . import config as config_mod
from .auth import sessions, users
from .auth.throttle import LoginThrottle
from .db.database import Database
from .http import csrf
from .http.request import HTTPError, Request
from .http.response import Response, apply_security_headers, error_json, guess_type, html
from .http.router import Router
from .http.templates import make_env
from .logs import fields

log = logging.getLogger("docwizard")

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class App:
    def __init__(self, cfg: config_mod.Config | None = None):
        self.cfg = cfg or config_mod.load()
        self.db = Database(self.cfg.db_path, self.cfg.server.durability)
        self.router = Router()
        self.templates = make_env()
        self.throttle = LoginThrottle()
        self.static_prefix = f"/static/{self.cfg.release}"
        self.started = False
        self._tasks: list[asyncio.Task] = []
        self._startup_hooks: list = []
        self._shutdown_hooks: list = []
        self._ws_handler = None
        self._register()

    def _register(self) -> None:
        from . import api, pages
        from .collab.authority import Rooms
        from .collab.hub import Hub
        from .projects.scanner import Scanner
        from .render.layouts import REPO_LAYOUTS, Layouts

        self.layouts = Layouts([REPO_LAYOUTS, self.cfg.data_dir / "layouts"], self.cfg.cache_dir)
        self.rooms = Rooms(self)
        self.scanner = Scanner(self)
        self._ws_handler = Hub(self)
        pages.register(self)
        api.register(self)

    # -- lifecycle ------------------------------------------------------
    def on_startup(self, fn) -> None:
        self._startup_hooks.append(fn)

    def on_shutdown(self, fn) -> None:
        self._shutdown_hooks.append(fn)

    def spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def startup(self) -> None:
        if self.started:
            return
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.db.open)
        for hook in self._startup_hooks:
            await hook()
        await self.scanner.startup()
        self.spawn(self.rooms.maintenance(), "rooms")
        self.spawn(self._session_cleanup(), "session-cleanup")
        self.started = True

    async def shutdown(self) -> None:
        try:
            await self.rooms.flush_all()
        except Exception:  # noqa: BLE001
            log.exception("flushing rooms failed")
        for hook in reversed(self._shutdown_hooks):
            try:
                await hook()
            except Exception:  # noqa: BLE001
                log.exception("shutdown hook failed")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.to_thread(self.db.close)
        self.started = False

    async def _session_cleanup(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.db.write(sessions.cleanup)
            except Exception:  # noqa: BLE001
                log.exception("session cleanup failed")

    # -- ASGI -----------------------------------------------------------
    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "http":
            await self.handle_http(scope, receive, send)
        elif kind == "websocket":
            if self._ws_handler is None:
                await send({"type": "websocket.close", "code": 1011})
            else:
                await self._ws_handler(scope, receive, send)
        elif kind == "lifespan":
            await self._lifespan(receive, send)

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.startup()
                except Exception as exc:  # noqa: BLE001
                    log.exception("startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def load_session(self, req: Request) -> None:
        token = req.cookies.get(self.cfg.cookie_name)
        if not token:
            return
        sess = await self.db.read(lambda c: sessions.load(c, token))
        if sess is None:
            return
        req.session = sess
        if sess.user_id is not None:
            req.user = await self.db.read(lambda c: users.get(c, sess.user_id))
            if req.user is None:
                req.session = None
                return
        if sessions.needs_touch(sess):
            idle = self.cfg.security.session_idle_days * 86400
            await self.db.write(lambda c: sessions.touch(c, sess, idle))

    async def handle_http(self, scope, receive, send) -> None:
        start = time.monotonic()
        req = Request(scope, receive)
        req.nonce = secrets.token_urlsafe(16)
        req.request_id = secrets.token_hex(8)
        try:
            resp = await self.dispatch(req)
        except HTTPError as err:
            resp = self.error_response(req, err)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error", extra=fields(request_id=req.request_id, route=req.path))
            resp = self.error_response(req, HTTPError(500, "internal", "Something went wrong on the server."))
        apply_security_headers(resp, req.nonce, self.cfg.preview_origin, self.cfg.secure_cookies)
        if req.method == "HEAD":
            resp.body = b""
            resp.file_path = None
        await resp.send(send)
        log.info("request", extra=fields(
            request_id=req.request_id, method=req.method, route=req.path, status=resp.status,
            user=req.user.id if req.user else None, ms=round((time.monotonic() - start) * 1000, 1)))

    async def dispatch(self, req: Request) -> Response:
        if self.cfg.server.serve_static and req.path.startswith("/static/"):
            return self.static(req)
        route, params = self.router.match(req)
        req.path_params = params
        opts = route.options
        if not opts.get("host_any") and req.host and self._is_preview_host(req) and not opts.get("preview_host"):
            raise HTTPError(404, "not_found")
        if not opts.get("no_session"):
            await self.load_session(req)
        if req.method not in csrf.SAFE_METHODS and not opts.get("csrf_exempt"):
            csrf.check_origin(req, self.cfg.app_origin)
            if req.session is None:
                if not opts.get("anonymous"):
                    raise HTTPError(401, "unauthenticated", "Log in to continue.")
            else:
                supplied = req.headers.get("x-csrf-token")
                if supplied is None and req.headers.get("content-type", "").startswith(
                        "application/x-www-form-urlencoded"):
                    supplied = (await req.form()).get("_csrf")
                csrf.check_token(supplied, req.session.csrf_token)
        if opts.get("auth", True) and not opts.get("anonymous") and req.user is None:
            if req.wants_json:
                raise HTTPError(401, "unauthenticated", "Log in to continue.")
            raise HTTPError(303, "login_required", headers=[("location", "/login?next=" + req.path)])
        return await route.handler(req)

    def _is_preview_host(self, req: Request) -> bool:
        preview = self.cfg.preview_origin
        return preview != self.cfg.app_origin and req.origin == preview

    def static(self, req: Request) -> Response:
        prefix = self.static_prefix + "/"
        if not req.path.startswith(prefix):
            raise HTTPError(404, "not_found")
        rel = req.path[len(prefix):]
        root = WEB_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file() or any(p.startswith(".") for p in Path(rel).parts):
            raise HTTPError(404, "not_found")
        ctype = guess_type(target.name)
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml", "application/json"):
            ctype += "; charset=utf-8"
        if target.suffix in (".js", ".mjs"):
            ctype = "text/javascript; charset=utf-8"
        resp = Response(b"", 200, ctype)
        resp.file_path = target
        resp.set_header("cache-control", "public, max-age=31536000, immutable")
        return resp

    def error_response(self, req: Request, err: HTTPError) -> Response:
        if 300 <= err.status < 400:
            resp = Response(b"", err.status, headers=[])
            for k, v in err.headers:
                resp.set_header(k, v)
            return resp
        if req.wants_json:
            return error_json(err)
        resp = html(self.render(req, "error.html", status=err.status, message=err.message), err.status)
        for k, v in err.headers:
            resp.set_header(k, v)
        return resp

    # -- templates ------------------------------------------------------
    def render(self, req: Request, name: str, **ctx) -> str:
        theme = req.user.theme if req.user else (req.cookies.get("theme") or "dark")
        base = {
            "user": req.user,
            "csrf": req.session.csrf_token if req.session else "",
            "nonce": req.nonce,
            "static": self.static_prefix,
            "release": self.cfg.release,
            "theme": theme if theme in ("dark", "light", "system") else "dark",
            "path": req.path,
        }
        base.update(ctx)
        return self.templates.get_template(name).render(**base)

    def page(self, req: Request, name: str, status: int = 200, **ctx) -> Response:
        return html(self.render(req, name, **ctx), status)


def create_app(cfg: config_mod.Config | None = None) -> App:
    return App(cfg)
