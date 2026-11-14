"""Two-step login with TOTP and recovery codes (§13.2).

After the password (or an OIDC login) succeeds for an account with two-step login
on, no session is created yet: a short-lived ticket in a cookie leads to the code
form, and only a correct code or an unused recovery code starts the session.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from ..auth import totp, users
from ..http.request import Request
from ..http.response import Response, redirect
from ..secretbox import context_for

TICKET_COOKIE = "docwizard_2fa"
TICKET_SECONDS = 300
SETUP_SECONDS = 900
MAX_ATTEMPTS = 5


def register(app) -> None:
    cfg = app.cfg
    tickets: dict[str, dict] = {}  # ticket -> {user, next, oidc, created, attempts}
    setups: dict[int, tuple[str, float]] = {}  # user id -> (secret, created)

    def _prune() -> None:
        now = time.time()
        for key in [k for k, t in tickets.items() if now - t["created"] > TICKET_SECONDS]:
            del tickets[key]
        for key in [k for k, s in setups.items() if now - s[1] > SETUP_SECONDS]:
            del setups[key]

    def totp_required(user) -> bool:
        return bool(user.require_totp or (user.is_admin and app.policy("require_totp_for_admins")))

    app.totp_required = totp_required

    async def finish_login(req: Request, user_id: int, next_url: str, oidc_provider: str | None = None) -> Response:
        """Start the session, or send the user to the code form first."""
        row = await app.db.fetchone("SELECT totp_secret_enc FROM users WHERE id = ?", (user_id,))
        if row is not None and row["totp_secret_enc"]:
            _prune()
            ticket = secrets.token_urlsafe(32)
            tickets[ticket] = {"user": user_id, "next": next_url, "oidc": oidc_provider, "created": time.time(),
                               "attempts": 0}
            resp = redirect("/login/totp")
            resp.set_cookie(TICKET_COOKIE, ticket, max_age=TICKET_SECONDS, secure=cfg.secure_cookies,
                            same_site="Strict")
            return resp
        resp = redirect(next_url)
        await asyncio.to_thread(app.start_session, req, resp, user_id, oidc_provider)
        await app.db.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'login', ?)",
                             (f"user:{user_id}", json.dumps({"ip": req.client_ip, "oidc": oidc_provider})))
        return resp

    app.finish_login = finish_login

    def ticket_of(req: Request):
        _prune()
        key = req.cookies.get(TICKET_COOKIE, "")
        return key, tickets.get(key)

    async def code_page(req: Request) -> Response:
        _, ticket = ticket_of(req)
        if ticket is None:
            return redirect("/login")
        return app.page(req, "login_totp.html", error=None)

    async def check_code(user_id: int, code: str) -> bool:
        row = await app.db.fetchone("SELECT totp_secret_enc, totp_last_counter FROM users WHERE id = ?", (user_id,))
        if row is None or not row["totp_secret_enc"]:
            return False
        if any(ch.isalpha() for ch in code):
            digest = totp.hash_recovery(code)

            def use(c):
                return c.execute("UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ?"
                                 " AND used_at IS NULL", (time.time(), user_id, digest)).rowcount == 1

            ok = await app.db.write(use)
            if ok:
                await app.db.execute("INSERT INTO audit_log(actor, action) VALUES (?, 'totp.recovery_code')",
                                     (f"user:{user_id}",))
            return ok
        secret = app.secrets.open(row["totp_secret_enc"], context_for("users", user_id))
        counter = totp.verify(secret, code, row["totp_last_counter"])
        if counter is None:
            return False

        def store(c):
            # Compare-and-set, so two requests can't both use the same step.
            return c.execute("UPDATE users SET totp_last_counter = ? WHERE id = ?"
                             " AND (totp_last_counter IS NULL OR totp_last_counter < ?)",
                             (counter, user_id, counter)).rowcount == 1

        return await app.db.write(store)

    async def code_post(req: Request) -> Response:
        key, ticket = ticket_of(req)
        if ticket is None:
            return redirect("/login?error=" + "Your login expired. Log in again.".replace(" ", "+"))
        form = await req.form()
        code = form.get("code", "").strip()
        if not app.throttle.allow(f"totp:{ticket['user']}", req.client_ip):
            return app.page(req, "login_totp.html", 429, error="Too many attempts. Wait a few minutes and try again.")
        if not await check_code(ticket["user"], code):
            ticket["attempts"] += 1
            if ticket["attempts"] >= MAX_ATTEMPTS:
                tickets.pop(key, None)
                resp = redirect("/login")
                resp.delete_cookie(TICKET_COOKIE, secure=cfg.secure_cookies)
                return resp
            return app.page(req, "login_totp.html", 400, error="This code isn't right. Try the current one.")
        tickets.pop(key, None)
        resp = redirect(ticket["next"])
        resp.delete_cookie(TICKET_COOKIE, secure=cfg.secure_cookies)
        await asyncio.to_thread(app.start_session, req, resp, ticket["user"], ticket["oidc"])
        await app.db.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'login', ?)",
                             (f"user:{ticket['user']}", json.dumps({"ip": req.client_ip, "totp": True,
                                                                    "oidc": ticket["oidc"]})))
        return resp

    # -- settings ------------------------------------------------------------
    def setup_ctx(user, secret: str) -> dict:
        from ..auth import qr

        uri = totp.provisioning_uri(secret, user.username, "DocWizard")
        return {"secret": totp.grouped(secret), "uri": uri, "qr": qr.svg(uri, label="Setup code for an authenticator app")}

    async def new_codes(user_id: int) -> list[str]:
        codes = totp.new_recovery_codes()

        def work(c):
            c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
            c.executemany("INSERT INTO recovery_codes(user_id, code_hash) VALUES (?, ?)",
                          [(user_id, totp.hash_recovery(code)) for code in codes])

        await app.db.write(work)
        return codes

    async def settings_totp(req: Request) -> Response:
        form = await req.form()
        user = req.user
        action = form.get("action", "")
        _prune()
        if action == "begin":
            secret = totp.new_secret()
            setups[user.id] = (secret, time.time())
            return await app.render_settings(req, totp_setup=setup_ctx(user, secret))
        if action == "enable":
            pending = setups.get(user.id)
            if pending is None:
                return await app.render_settings(req, 400, totp_error="The setup expired. Start again.")
            counter = totp.verify(pending[0], form.get("code", ""))
            if counter is None:
                return await app.render_settings(req, 400, totp_setup=setup_ctx(user, pending[0]),
                                                 totp_error="This code isn't right. Check the time on your phone "
                                                            "and enter the current code.")
            sealed = app.secrets.seal(pending[0], context_for("users", user.id))
            await app.db.execute("UPDATE users SET totp_secret_enc = ?, totp_last_counter = ? WHERE id = ?",
                                 (sealed, counter, user.id))
            del setups[user.id]
            codes = await new_codes(user.id)
            await app.db.execute("INSERT INTO audit_log(actor, action) VALUES (?, 'totp.enable')", (f"user:{user.id}",))
            req.user = await app.db.read(lambda c: users.get(c, user.id))
            return await app.render_settings(req, recovery_codes=codes, saved="totp")
        if action == "recovery":
            if not await check_code(user.id, form.get("code", "")):
                return await app.render_settings(req, 400, totp_error="This code isn't right.")
            return await app.render_settings(req, recovery_codes=await new_codes(user.id))
        if action == "disable":
            if totp_required(user):
                return await app.render_settings(req, 400, totp_error="Two-step login is required for your account.")
            row = await app.db.read(lambda c: users.get_by_username(c, user.username))
            if row["password_hash"]:
                ok = await asyncio.to_thread(users.authenticate, row, form.get("password", ""))
            else:
                ok = await check_code(user.id, form.get("code", ""))
            if not ok:
                return await app.render_settings(req, 400, totp_error="The password is wrong.")

            def off(c):
                c.execute("UPDATE users SET totp_secret_enc = NULL, totp_last_counter = NULL WHERE id = ?", (user.id,))
                c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user.id,))
                c.execute("INSERT INTO audit_log(actor, action) VALUES (?, 'totp.disable')", (f"user:{user.id}",))

            await app.db.write(off)
            return redirect("/settings?saved=totp-off#totp")
        return redirect("/settings#totp")

    app.router.add("GET", "/login/totp", code_page, auth=False)
    app.router.add("POST", "/login/totp", code_post, anonymous=True)
    app.router.add("POST", "/settings/totp", settings_totp, totp_exempt=True)
