"""OIDC login, account linking and logout (§13.4)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import quote

from ..auth import users
from ..auth.oidc import OIDCError, groups_of
from ..http.request import Request
from ..http.response import Response, redirect
from .account import safe_next

STATE_COOKIE = "docwizard_oidc"


def _username_from(value: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", value.split("@")[0]).strip("._-")[:36]
    if len(base) < 2:
        base = "user"
    if not base[0].isalnum():
        base = "u" + base
    return base


def register(app) -> None:
    cfg = app.cfg

    def fail(message: str) -> Response:
        return redirect("/login?error=" + quote(message))

    async def start(req: Request) -> Response:
        provider_id = req.param("provider")
        link = req.query.get("link") == "1" and req.user is not None
        try:
            url, pending = await app.oidc.start(provider_id, safe_next(req.query.get("next")),
                                                req.user.id if link else None)
        except OIDCError as exc:
            return fail(str(exc))
        resp = redirect(url)
        # Binds the login to this browser: a callback with someone else's state is refused.
        resp.set_cookie(STATE_COOKIE, pending.state, max_age=600, secure=cfg.secure_cookies)
        return resp

    async def callback(req: Request) -> Response:
        provider_id = req.param("provider")
        state = req.query.get("state", "")
        if req.query.get("error"):
            app.oidc.pending.pop(state, None)
            return fail("The login was cancelled or refused by the login provider.")
        if not state or req.cookies.get(STATE_COOKIE) != state:
            return fail("This login didn't start in this browser. Try again.")
        try:
            pending, claims = await app.oidc.finish(provider_id, state, req.query.get("code", ""))
        except OIDCError as exc:
            return fail(str(exc))
        provider = app.oidc.get(provider_id)
        groups = groups_of(provider, claims)
        if provider.allowed_groups and not groups.intersection(provider.allowed_groups):
            return fail("Your account isn't in a group that may use DocWizard.")
        issuer, subject = provider.issuer, claims["sub"]
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        identity = await app.db.fetchone("SELECT * FROM identities WHERE issuer = ? AND subject = ?", (issuer, subject))

        if pending.link_user is not None:
            if req.user is None or req.user.id != pending.link_user:
                return fail("Log in again to link this account.")
            if identity is not None and identity["user_id"] != req.user.id:
                return redirect("/settings?error=" + quote("This login is already linked to another account."))
            if identity is None:
                await app.db.execute(
                    "INSERT INTO identities(user_id, provider, issuer, subject, email, last_login_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)", (req.user.id, provider.id, issuer, subject, email, time.time()))
                await app.db.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'oidc.link', ?)",
                                     (f"user:{req.user.id}", json.dumps({"provider": provider.id})))
            resp = redirect("/settings?saved=linked#logins")
            resp.delete_cookie(STATE_COOKIE, secure=cfg.secure_cookies)
            return resp

        if identity is not None:
            user_id = identity["user_id"]
            active = await app.db.fetchone("SELECT active FROM users WHERE id = ?", (user_id,))
            if not active or not active["active"]:
                return fail("This account is disabled.")
        else:
            # Never matched by email: linking to an existing account needs that account's login.
            if not provider.auto_create or app.policy("registration") == "closed":
                return fail("There's no DocWizard account for this login. Ask an admin, or log in and link it in "
                            "your settings.")
            wanted = claims.get(provider.username_claim) or email or subject
            display = claims.get("name") if isinstance(claims.get("name"), str) else ""

            def create(c):
                base = _username_from(str(wanted))
                name, n = base, 1
                while users.get_by_username(c, name) is not None:
                    n += 1
                    name = f"{base}{n}"
                uid = users.create(c, name, None, display_name=(display or name)[:80], email=email)
                c.execute("INSERT INTO identities(user_id, provider, issuer, subject, email) VALUES (?, ?, ?, ?, ?)",
                          (uid, provider.id, issuer, subject, email))
                c.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'oidc.create', ?)",
                          (f"user:{uid}", json.dumps({"provider": provider.id})))
                return uid

            user_id = await app.db.write(create)

        def touch(c):
            c.execute("UPDATE identities SET last_login_at = ?, email = COALESCE(?, email)"
                      " WHERE issuer = ? AND subject = ?", (time.time(), email, issuer, subject))
            if provider.admin_groups:
                # Re-evaluated at every login (§13.4).
                role = "admin" if groups.intersection(provider.admin_groups) else "user"
                c.execute("UPDATE users SET instance_role = ? WHERE id = ?", (role, user_id))
            c.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'login', ?)",
                      (f"user:{user_id}", json.dumps({"ip": req.client_ip, "provider": provider.id})))

        await app.db.write(touch)
        resp = redirect(pending.next)
        await asyncio.to_thread(app.start_session, req, resp, user_id, provider.id)
        resp.delete_cookie(STATE_COOKIE, secure=cfg.secure_cookies)
        return resp

    async def unlink(req: Request) -> Response:
        form = await req.form()
        iid = form.get("identity_id", "")
        uid = req.user.id
        has_password = (await app.db.fetchone("SELECT password_hash FROM users WHERE id = ?", (uid,)))["password_hash"]
        count = (await app.db.fetchone("SELECT count(*) AS n FROM identities WHERE user_id = ?", (uid,)))["n"]
        if not has_password and count <= 1:
            return redirect("/settings?error=" + quote("Set a password first: this is your only way to log in."))
        await app.db.execute("DELETE FROM identities WHERE id = ? AND user_id = ?",
                             (int(iid) if iid.isdigit() else -1, uid))
        return redirect("/settings#logins")

    app.router.add("GET", "/login/oidc/{provider}", start, auth=False)
    app.router.add("GET", "/login/oidc/{provider}/callback", callback, auth=False)
    app.router.add("POST", "/settings/identities", unlink)
