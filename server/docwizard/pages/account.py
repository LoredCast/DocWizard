"""Login, logout, registration and personal settings."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

from ..auth import captcha, passwords, sessions, users
from ..http.request import HTTPError, Request
from ..http.response import Response, json_response, redirect
from ..mail import MailError


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
                        providers=app.oidc.list(), verified=req.query.get("verified") == "1",
                        deleted=req.query.get("deleted") == "1")

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

    def register_form(req: Request, status: int = 200, **ctx) -> Response:
        ctx.setdefault("error", None)
        resp = app.page(req, "register.html", status, min_length=app.policy("min_password_length"),
                        email_required=bool(app.policy("require_email_verification")), mail=app.mail.enabled,
                        captcha=captcha.widget(cfg), **ctx)
        return captcha.with_widget_csp(resp, cfg, req.nonce, cfg.preview_origin)

    async def register_page(req: Request) -> Response:
        if app.policy("registration") != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        return register_form(req, username="", display_name="", email="")

    async def register_post(req: Request) -> Response:
        if app.policy("registration") != "open":
            raise HTTPError(404, "not_found", "Registration is closed on this server.")
        form = await req.form()
        username = form.get("username", "").strip()
        display_name = form.get("display_name", "").strip()[:80]
        password = form.get("password", "")
        raw_email = form.get("email", "").strip()[:200]
        ctx = {"username": username, "display_name": display_name, "email": raw_email}
        if not app.throttle.allow_register(req.client_ip, raw_email):
            return register_form(req, 429, error="Too many attempts. Try again later.", **ctx)
        if not await captcha.verify(app, form, req.client_ip):
            return register_form(req, 400, error="Please confirm that you're not a robot.", **ctx)
        try:
            email = users.clean_email(raw_email)
        except ValueError as exc:
            return register_form(req, 400, error=str(exc), **ctx)
        if email is None and app.policy("require_email_verification"):
            return register_form(req, 400, error="Enter your email address.", **ctx)
        problem = passwords.check_policy(password, app.policy("min_password_length"), username)
        if problem:
            return register_form(req, 400, error=problem, **ctx)
        pw_hash = await asyncio.to_thread(passwords.hash_password, password)

        def create(c):
            if email is not None and users.email_taken(c, email):
                raise ValueError("Another account uses this email address. Log in, or reset its password.")
            uid = users.create(c, username, pw_hash, display_name=display_name, email=email)
            c.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'user.register', ?)",
                      (f"user:{uid}", json.dumps({"email": email is not None})))
            return uid

        try:
            user_id = await app.db.write(create)
        except ValueError as exc:
            return register_form(req, 400, error=str(exc), **ctx)
        app.registrations += 1
        target = "/"
        if email is not None and app.mail.enabled:
            try:
                await app.mail_verify_link(user_id, email)
                target = "/?verify=sent"
            except MailError:
                target = "/?verify=failed"
        resp = redirect(target)
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
        from ..api.grammar import available as grammar_available

        ctx["grammar"] = grammar_available(app)
        from ..api import assist

        ctx["assist"] = {"enabled": assist.enabled(app), "model": app.cfg.assist.model}
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
        pending = await app.db.fetchone(
            "SELECT pending_email FROM account_tokens WHERE user_id = ? AND purpose = 'verify' AND used_at IS NULL"
            " AND expires_at > ?", (req.user.id, time.time()))
        ctx["email_pending"] = pending["pending_email"] if pending else None
        ctx.setdefault("email_error", None)
        ctx["mail"] = app.mail.enabled
        ctx["email_required"] = bool(app.policy("require_email_verification"))
        ctx["storage"] = await app.quotas.usage(req.user.id)
        await app.settings_extras(req, ctx)
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
        from ..api.grammar import CHOICES as GRAMMAR_CHOICES

        if form.get("grammar") in GRAMMAR_CHOICES:
            prefs["grammar"] = form["grammar"]
        if form.get("assist_shown"):
            prefs["assist"] = form.get("assist") == "on"
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

    async def settings_email(req: Request) -> Response:
        """Change, confirm again or remove the account's email address (§13.2).

        With mail, a new address only replaces the old one once its link is used, so a typo
        or someone else's address never takes over password resets."""
        form = await req.form()
        user = req.user
        action = form.get("action", "change")
        if action == "resend":
            if not user.email or user.email_verified:
                return redirect("/settings#email")
            if not app.throttle.allow_register(req.client_ip, user.email):
                return await render_settings(req, 429, email_error="Too many attempts. Try again later.")
            try:
                await app.mail_verify_link(user.id, user.email)
            except MailError as exc:
                return await render_settings(req, 400, email_error=str(exc))
            return redirect("/settings?saved=email-sent#email")
        row = await app.db.read(lambda c: users.get_by_username(c, user.username))
        if row["password_hash"] is not None and not await asyncio.to_thread(
                users.authenticate, row, form.get("current", "")):
            return await render_settings(req, 400, email_error="The current password is wrong.")
        if action == "remove":
            if app.policy("require_email_verification") and not user.is_admin:
                return await render_settings(req, 400, email_error="This server needs an email address for every"
                                                                   " account.")
            await app.db.write(lambda c: users.set_email(c, user.id, None, verified=False))
            return redirect("/settings?saved=email#email")
        try:
            email = users.clean_email(form.get("email", ""))
        except ValueError as exc:
            return await render_settings(req, 400, email_error=str(exc))
        if email is None:
            return await render_settings(req, 400, email_error="Enter your email address.")
        if user.email and email.lower() == user.email.lower() and user.email_verified:
            return redirect("/settings#email")
        if await app.db.read(lambda c: users.email_taken(c, email, exclude_user=user.id)):
            return await render_settings(req, 400, email_error="Another account uses this email address.")
        if not app.mail.enabled:
            # Nothing can confirm it; it is used for display only.
            await app.db.write(lambda c: users.set_email(c, user.id, email, verified=False))
            return redirect("/settings?saved=email#email")
        if not app.throttle.allow_register(req.client_ip, email):
            return await render_settings(req, 429, email_error="Too many attempts. Try again later.")
        if not user.email:
            # A first address is kept right away, unconfirmed, so the reminder can resend the link.
            await app.db.write(lambda c: users.set_email(c, user.id, email, verified=False))
        try:
            await app.mail_verify_link(user.id, email)
        except MailError as exc:
            return await render_settings(req, 400, email_error=str(exc))
        return redirect("/settings?saved=email-sent#email")

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
    app.router.add("POST", "/settings/email", settings_email, totp_exempt=True)
    app.router.add("GET", "/api/me", me, guest=True, totp_exempt=True)
