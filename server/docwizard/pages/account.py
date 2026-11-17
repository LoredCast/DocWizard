"""Login, logout, registration and personal settings."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

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

    def start_session(req: Request, resp: Response, user_id: int, oidc_provider: str | None = None) -> None:
        old = req.session

        def work(c):
            if old is not None:
                sessions.delete(c, old)  # rotate at login
            token, sess = sessions.create(c, user_id, idle, req.headers.get("user-agent", ""))
            if oidc_provider:
                c.execute("UPDATE sessions SET oidc_provider = ? WHERE token_hash = ?", (oidc_provider, sess.token_hash))
            return token, sess

        token, _ = app.db.write_sync(work)
        resp.set_cookie(cfg.cookie_name, token, max_age=idle, secure=cfg.secure_cookies)

    app.start_session = start_session

    async def login_page(req: Request) -> Response:
        if req.user:
            return redirect(safe_next(req.query.get("next")))
        return app.page(req, "login.html", next=safe_next(req.query.get("next")), username="",
                        error=req.query.get("error"), registration_open=app.policy("registration") == "open",
                        providers=app.oidc.list())

    async def login(req: Request) -> Response:
        form = await req.form()
        username = form.get("username", "").strip()
        password = form.get("password", "")
        nxt = safe_next(form.get("next"))
        ctx = {"next": nxt, "username": username, "registration_open": app.policy("registration") == "open",
               "providers": app.oidc.list()}
        if not app.throttle.allow(username, req.client_ip):
            return app.page(req, "login.html", 429, error="Too many attempts. Wait a few minutes and try again.", **ctx)
        row = await app.db.read(lambda c: users.get_by_username(c, username))
        ok = await asyncio.to_thread(users.authenticate, row, password)
        if not ok:
            return app.page(req, "login.html", 400, error="The username or password is wrong.", **ctx)
        if passwords.needs_rehash(row["password_hash"]):
            new_hash = await asyncio.to_thread(passwords.hash_password, password)
            await app.db.write(lambda c: users.set_password(c, row["id"], new_hash))
        return await app.finish_login(req, row["id"], nxt)

    async def logout(req: Request) -> Response:
        target = "/login"
        if req.session is not None:
            sess = req.session
            row = await app.db.fetchone("SELECT oidc_provider FROM sessions WHERE token_hash = ?", (sess.token_hash,))
            await app.db.write(lambda c: sessions.delete(c, sess))
            if row is not None and row["oidc_provider"]:
                # RP-initiated logout at the provider, when it offers one (§13.4).
                target = await app.oidc.logout_url(row["oidc_provider"]) or target
        resp = redirect(target)
        resp.delete_cookie(cfg.cookie_name, secure=cfg.secure_cookies)
        return resp

    async def register_page(req: Request) -> Response:
        if app.policy("registration") != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        return app.page(req, "register.html", username="", display_name="", error=None,
                        min_length=app.policy("min_password_length"))

    async def register_post(req: Request) -> Response:
        if app.policy("registration") != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        form = await req.form()
        username = form.get("username", "").strip()
        display_name = form.get("display_name", "").strip()[:80]
        password = form.get("password", "")
        ctx = {"username": username, "display_name": display_name, "min_length": app.policy("min_password_length")}
        if not app.throttle.allow("register", req.client_ip):
            return app.page(req, "register.html", 429, error="Too many attempts. Try again later.", **ctx)
        problem = passwords.check_policy(password, app.policy("min_password_length"), username)
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
        return await render_settings(req, saved=req.query.get("saved"), error=None,
                                     link_error=req.query.get("error"))

    async def render_settings(req: Request, status: int = 200, **ctx) -> Response:
        rows = await app.db.fetchall(
            "SELECT s.id, s.name, s.library_type, s.library_id, s.collection, s.status, s.last_error, s.last_sync_at,"
            " s.api_url, s.storage_enc IS NOT NULL AS has_storage,"
            " (SELECT COUNT(*) FROM citation_items i WHERE i.source_id = s.id) AS item_count"
            " FROM citation_sources s WHERE s.owner_id = ? ORDER BY s.name", (req.user.id,))
        ids = await app.db.fetchall("SELECT id, provider, email, last_login_at FROM identities WHERE user_id = ?"
                                    " ORDER BY provider", (req.user.id,))
        linked = {r["provider"] for r in ids}
        ctx["identities"] = [{**dict(r), "label": app.oidc.by_id[r["provider"]].label
                              if r["provider"] in app.oidc.by_id else r["provider"]} for r in ids]
        ctx["linkable"] = [p for p in app.oidc.list() if p.id not in linked]
        ctx.setdefault("zotero_error", None)
        ctx.setdefault("link_error", None)
        ctx.setdefault("zotero_values", {})
        ctx.setdefault("totp_setup", None)
        ctx.setdefault("totp_error", None)
        ctx.setdefault("recovery_codes", None)
        ctx.setdefault("saved", req.query.get("saved"))
        ctx.setdefault("error", None)
        ctx["totp_required"] = app.totp_required(req.user)
        ctx["totp_notice"] = req.query.get("totp") == "required" and not req.user.totp
        ctx["recovery_left"] = (await app.db.fetchone(
            "SELECT COUNT(*) AS n FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (req.user.id,)))["n"]
        ctx["has_password"] = (await app.db.fetchone(
            "SELECT password_hash IS NOT NULL AS p FROM users WHERE id = ?", (req.user.id,)))["p"]
        from ..i18n import LANGUAGES

        return app.page(req, "settings.html", status, sources=[{**dict(r), "api_host": urlsplit(r["api_url"]).netloc if r["api_url"] else ""} for r in rows], languages=LANGUAGES, **ctx)

    app.render_settings = render_settings

    async def settings_profile(req: Request) -> Response:
        form = await req.form()
        user = req.user
        prefs = dict(user.preferences)
        if form.get("theme") in ("dark", "light", "system"):
            prefs["theme"] = form["theme"]
        if form.get("editor_font") in ("sans", "mono"):
            prefs["editor_font"] = form["editor_font"]
        from ..i18n import LANGUAGES

        if form.get("language") in LANGUAGES:
            prefs["language"] = form["language"]
        from ..notifications import EMAIL_CHOICES

        if form.get("notify_email") in EMAIL_CHOICES:
            prefs["notify_email"] = form["notify_email"]
        if form.get("editor_size", "").isdigit() and 15 <= int(form["editor_size"]) <= 20:
            prefs["editor_size"] = int(form["editor_size"])
        name = form.get("display_name", "").strip()[:80] or user.display_name
        await app.db.write(lambda c: users.update_profile(c, user.id, name, prefs))
        resp = redirect("/settings?saved=profile")
        resp.set_cookie("theme", prefs.get("theme", "dark"), max_age=365 * 86400, secure=cfg.secure_cookies)
        if prefs.get("language"):
            resp.set_cookie("lang", prefs["language"], max_age=365 * 86400, secure=cfg.secure_cookies)
        return resp

    async def settings_password(req: Request) -> Response:
        form = await req.form()
        user = req.user
        row = await app.db.read(lambda c: users.get_by_username(c, user.username))
        if not await asyncio.to_thread(users.authenticate, row, form.get("current", "")):
            return await render_settings(req, 400, saved=None, error="The current password is wrong.")
        problem = passwords.check_policy(form.get("new", ""), app.policy("min_password_length"), user.username)
        if problem:
            return await render_settings(req, 400, saved=None, error=problem)
        new_hash = await asyncio.to_thread(passwords.hash_password, form["new"])
        keep = req.session.token_hash

        def work(c):
            users.set_password(c, user.id, new_hash)
            sessions.delete_for_user(c, user.id, keep=keep)

        await app.db.write(work)
        return redirect("/settings?saved=password")

    async def me(req: Request) -> Response:
        who = req.user.public() if req.user is not None else req.guest.public()
        return json_response({"user": who, "csrf": req.session.csrf_token})

    app.router.add("GET", "/login", login_page, auth=False)
    app.router.add("POST", "/login", login, anonymous=True)
    app.router.add("POST", "/logout", logout, anonymous=True, totp_exempt=True)
    app.router.add("GET", "/register", register_page, auth=False)
    app.router.add("POST", "/register", register_post, anonymous=True)
    app.router.add("GET", "/settings", settings_page, totp_exempt=True)
    app.router.add("POST", "/settings/profile", settings_profile)
    app.router.add("POST", "/settings/password", settings_password, totp_exempt=True)
    app.router.add("GET", "/api/me", me, guest=True, totp_exempt=True)
