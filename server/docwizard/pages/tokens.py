"""Password reset and invitation links (§13.2).

Tokens are single use and stored hashed: a reset link is valid for an hour, an
invitation for a week. With mail set up, links are emailed; otherwise admins
see them in the admin area and pass them on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time

from ..auth import passwords, sessions, users
from ..http.request import HTTPError, Request
from ..http.response import Response
from ..mail import MailError

LIFETIME = {"reset": 3600, "invite": 7 * 86400}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(app) -> None:
    cfg = app.cfg

    async def issue(user_id: int, purpose: str, created_by: int | None = None) -> str:
        """A new link for the user; older unused links of the same kind stop working."""
        token = secrets.token_urlsafe(32)

        def work(c):
            c.execute("DELETE FROM account_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose))
            c.execute("INSERT INTO account_tokens(token_hash, user_id, purpose, expires_at, created_by)"
                      " VALUES (?, ?, ?, ?, ?)", (_hash(token), user_id, purpose, time.time() + LIFETIME[purpose],
                                                  created_by))

        await app.db.write(work)
        return f"{cfg.server.public_url.rstrip('/')}/reset/{token}"

    app.issue_account_link = issue

    async def mail_link(user_id: int, purpose: str, created_by: int | None = None) -> str:
        """Issue a link and email it. Returns the link; raises MailError when it can't be sent."""
        row = await app.db.fetchone("SELECT username, display_name, email FROM users WHERE id = ?", (user_id,))
        if row is None or not row["email"]:
            raise MailError("This account has no email address.")
        url = await issue(user_id, purpose, created_by)
        if purpose == "invite":
            subject = "You're invited to DocWizard"
            body = (f"Hello {row['display_name']},\n\nyou have an account on DocWizard at {cfg.server.public_url}"
                    f" with the username {row['username']}. Choose a password here to start:\n\n{url}\n\n"
                    "This link works once and for 7 days.\n")
        else:
            subject = "Reset your DocWizard password"
            body = (f"Hello {row['display_name']},\n\nsomeone asked to reset the password of the DocWizard account"
                    f" {row['username']}. Choose a new password here:\n\n{url}\n\nThis link works once and for an hour."
                    " If you didn't ask for this, ignore this message: your password stays the same.\n")
        await app.mail.send(row["email"], subject, body)
        return url

    app.mail_account_link = mail_link

    async def load(token: str):
        row = await app.db.fetchone(
            "SELECT t.*, u.username, u.display_name, u.active FROM account_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.token_hash = ?", (_hash(token),))
        if row is None or row["used_at"] is not None or row["expires_at"] < time.time() or not row["active"]:
            raise HTTPError(404, "not_found", "This link has expired or was already used. Ask for a new one.")
        return row

    async def forgot_page(req: Request) -> Response:
        return app.page(req, "forgot.html", sent=False, mail=app.mail.enabled, error=None)

    async def forgot_post(req: Request) -> Response:
        form = await req.form()
        ident = form.get("identifier", "").strip()
        if not app.mail.enabled:
            return app.page(req, "forgot.html", sent=False, mail=False, error=None)
        if not app.throttle.allow("forgot", req.client_ip):
            return app.page(req, "forgot.html", 429, sent=False, mail=True,
                            error="Too many attempts. Wait a few minutes and try again.")
        row = await app.db.fetchone("SELECT id, email FROM users WHERE active = 1 AND password_hash IS NOT NULL"
                                    " AND (username = ? OR (email IS NOT NULL AND lower(email) = lower(?)))",
                                    (ident, ident))
        if row is not None and row["email"]:
            try:
                await mail_link(row["id"], "reset")
                await app.db.execute("INSERT INTO audit_log(actor, action, meta_json) VALUES (?, 'password.forgot', ?)",
                                     (f"user:{row['id']}", json.dumps({"ip": req.client_ip})))
            except MailError:
                pass  # logged; the answer stays the same so it reveals nothing about accounts
        return app.page(req, "forgot.html", sent=True, mail=True, error=None)

    async def reset_page(req: Request) -> Response:
        row = await load(req.param("token"))
        return app.page(req, "reset.html", purpose=row["purpose"], username=row["username"],
                        display_name=row["display_name"], error=None, min_length=app.policy("min_password_length"))

    async def reset_post(req: Request) -> Response:
        token = req.param("token")
        row = await load(token)
        form = await req.form()
        password = form.get("password", "")
        display_name = form.get("display_name", "").strip()[:80] or row["display_name"]
        ctx = {"purpose": row["purpose"], "username": row["username"], "display_name": display_name,
               "min_length": app.policy("min_password_length")}
        problem = passwords.check_policy(password, app.policy("min_password_length"), row["username"])
        if problem:
            return app.page(req, "reset.html", 400, error=problem, **ctx)
        pw_hash = await asyncio.to_thread(passwords.hash_password, password)
        user_id = row["user_id"]

        def work(c):
            used = c.execute("UPDATE account_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                             (time.time(), _hash(token))).rowcount
            if not used:
                return False
            users.set_password(c, user_id, pw_hash)
            if row["purpose"] == "invite":
                c.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
            sessions.delete_for_user(c, user_id)
            c.execute("INSERT INTO audit_log(actor, action) VALUES (?, ?)",
                      (f"user:{user_id}", "password.reset" if row["purpose"] == "reset" else "invite.accept"))
            return True

        if not await app.db.write(work):
            raise HTTPError(404, "not_found", "This link was already used.")
        return await app.finish_login(req, user_id, "/")

    app.router.add("GET", "/forgot", forgot_page, auth=False)
    app.router.add("POST", "/forgot", forgot_post, anonymous=True)
    app.router.add("GET", "/reset/{token}", reset_page, auth=False)
    app.router.add("POST", "/reset/{token}", reset_post, anonymous=True)
