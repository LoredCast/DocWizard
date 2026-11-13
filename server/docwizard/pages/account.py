"""Login, logout, registration and personal settings."""

from __future__ import annotations

import asyncio
import json

from ..auth import passwords, sessions, users
from ..http.request import HTTPError, Request
from ..http.response import Response, json_response, redirect


def safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def register(app) -> None:
    cfg = app.cfg
    idle = cfg.security.session_idle_days * 86400

    def start_session(req: Request, resp: Response, user_id: int) -> None:
        old = req.session

        def work(c):
            if old is not None:
                sessions.delete(c, old)  # rotate at login
            return sessions.create(c, user_id, idle, req.headers.get("user-agent", ""))

        token, _ = app.db.write_sync(work)
        resp.set_cookie(cfg.cookie_name, token, max_age=idle, secure=cfg.secure_cookies)

    async def login_page(req: Request) -> Response:
        if req.user:
            return redirect(safe_next(req.query.get("next")))
        return app.page(req, "login.html", next=safe_next(req.query.get("next")), username="", error=None,
                        registration_open=cfg.accounts.registration == "open")

    async def login(req: Request) -> Response:
        form = await req.form()
        username = form.get("username", "").strip()
        password = form.get("password", "")
        nxt = safe_next(form.get("next"))
        ctx = {"next": nxt, "username": username, "registration_open": cfg.accounts.registration == "open"}
        if not app.throttle.allow(username, req.client_ip):
            return app.page(req, "login.html", 429, error="Too many attempts. Wait a few minutes and try again.", **ctx)
        row = await app.db.read(lambda c: users.get_by_username(c, username))
        ok = await asyncio.to_thread(users.authenticate, row, password)
        if not ok:
            return app.page(req, "login.html", 400, error="The username or password is wrong.", **ctx)
        if passwords.needs_rehash(row["password_hash"]):
            new_hash = await asyncio.to_thread(passwords.hash_password, password)
            await app.db.write(lambda c: users.set_password(c, row["id"], new_hash))
        resp = redirect(nxt)
        await asyncio.to_thread(start_session, req, resp, row["id"])
        await app.db.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'login', ?)",
                             (f"user:{row['id']}", json.dumps({"ip": req.client_ip})))
        return resp

    async def logout(req: Request) -> Response:
        if req.session is not None:
            sess = req.session
            await app.db.write(lambda c: sessions.delete(c, sess))
        resp = redirect("/login")
        resp.delete_cookie(cfg.cookie_name, secure=cfg.secure_cookies)
        return resp

    async def register_page(req: Request) -> Response:
        if cfg.accounts.registration != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        return app.page(req, "register.html", username="", display_name="", error=None,
                        min_length=cfg.accounts.min_password_length)

    async def register_post(req: Request) -> Response:
        if cfg.accounts.registration != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        form = await req.form()
        username = form.get("username", "").strip()
        display_name = form.get("display_name", "").strip()[:80]
        password = form.get("password", "")
        ctx = {"username": username, "display_name": display_name, "min_length": cfg.accounts.min_password_length}
        if not app.throttle.allow("register", req.client_ip):
            return app.page(req, "register.html", 429, error="Too many attempts. Try again later.", **ctx)
        problem = passwords.check_policy(password, cfg.accounts.min_password_length, username)
        if problem:
            return app.page(req, "register.html", 400, error=problem, **ctx)
        pw_hash = await asyncio.to_thread(passwords.hash_password, password)
        try:
            user_id = await app.db.write(lambda c: users.create(c, username, pw_hash, display_name=display_name))
        except ValueError as exc:
            return app.page(req, "register.html", 400, error=str(exc), **ctx)
        resp = redirect("/")
        await asyncio.to_thread(start_session, req, resp, user_id)
        return resp

    async def settings_page(req: Request) -> Response:
        return app.page(req, "settings.html", saved=req.query.get("saved"), error=None)

    async def settings_profile(req: Request) -> Response:
        form = await req.form()
        user = req.user
        prefs = dict(user.preferences)
        if form.get("theme") in ("dark", "light", "system"):
            prefs["theme"] = form["theme"]
        if form.get("editor_font") in ("sans", "mono"):
            prefs["editor_font"] = form["editor_font"]
        if form.get("editor_size", "").isdigit() and 15 <= int(form["editor_size"]) <= 20:
            prefs["editor_size"] = int(form["editor_size"])
        name = form.get("display_name", "").strip()[:80] or user.display_name
        await app.db.write(lambda c: users.update_profile(c, user.id, name, prefs))
        resp = redirect("/settings?saved=profile")
        resp.set_cookie("theme", prefs.get("theme", "dark"), max_age=365 * 86400, secure=cfg.secure_cookies)
        return resp

    async def settings_password(req: Request) -> Response:
        form = await req.form()
        user = req.user
        row = await app.db.read(lambda c: users.get_by_username(c, user.username))
        if not await asyncio.to_thread(users.authenticate, row, form.get("current", "")):
            return app.page(req, "settings.html", 400, saved=None, error="The current password is wrong.")
        problem = passwords.check_policy(form.get("new", ""), cfg.accounts.min_password_length, user.username)
        if problem:
            return app.page(req, "settings.html", 400, saved=None, error=problem)
        new_hash = await asyncio.to_thread(passwords.hash_password, form["new"])
        keep = req.session.token_hash

        def work(c):
            users.set_password(c, user.id, new_hash)
            sessions.delete_for_user(c, user.id, keep=keep)

        await app.db.write(work)
        return redirect("/settings?saved=password")

    async def me(req: Request) -> Response:
        return json_response({"user": req.user.public(), "csrf": req.session.csrf_token})

    app.router.add("GET", "/login", login_page, auth=False)
    app.router.add("POST", "/login", login, anonymous=True)
    app.router.add("POST", "/logout", logout, anonymous=True)
    app.router.add("GET", "/register", register_page, auth=False)
    app.router.add("POST", "/register", register_post, anonymous=True)
    app.router.add("GET", "/settings", settings_page)
    app.router.add("POST", "/settings/profile", settings_profile)
    app.router.add("POST", "/settings/password", settings_password)
    app.router.add("GET", "/api/me", me)
