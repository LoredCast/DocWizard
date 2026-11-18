"""Share-link landing page (§14.1): members join, everyone else becomes a guest."""

from __future__ import annotations

import asyncio
import json

from ..auth import passwords, permissions, sessions
from ..http.request import HTTPError, Request
from ..http.response import Response, redirect
from ..projects import service as svc
from ..render.scheduler import MAIN_CANDIDATES
from ..sharing import links
from .projects import doc_url


def register(app) -> None:
    cfg = app.cfg

    async def find(token: str):
        link = await app.db.read(lambda c: links.find(c, token))
        if link is None:
            raise HTTPError(404, "link_expired", "This link expired or was revoked. Ask for a new one.")
        return link

    async def landing(req: Request) -> Response:
        token = req.param("token")
        link = await find(token)
        if req.user is not None:
            return await join(req, link)
        if req.guest is not None and req.guest.project_id == link["project_id"]:
            return redirect(await start_url(link["project_id"], link["review"]))
        return app.page(req, "share.html", link=link, token=token, error=None, guest_name="",
                        guests=app.policy("guests"))

    async def join(req: Request, link) -> Response:
        """Logged-in users become members with the link's role, or keep a higher one."""
        uid = req.user.id
        role = links.cap_role(link["role"], app.policy("guest_max_role"))

        def work(c):
            current = svc.role_of(c, uid, link["project_id"])
            if permissions.rank(current) >= permissions.rank(role):
                return
            c.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?, ?, ?)"
                      " ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role",
                      (link["project_id"], uid, role))
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'share_link.join', ?, ?)",
                      (f"user:{uid}", link["project_id"], json.dumps({"link": link["id"], "role": role})))

        await app.db.write(work)
        return redirect(await start_url(link["project_id"], link["review"]))

    async def start_url(project_id: str, review: bool = False) -> str:
        docs = await app.db.fetchall("SELECT path FROM documents WHERE project_id = ? AND deleted_at IS NULL",
                                     (project_id,))
        paths = {d["path"] for d in docs}
        for name in MAIN_CANDIDATES:
            if name in paths:
                return doc_url(project_id, name) + ("?view=review" if review else "")
        return f"/p/{project_id}"

    async def enter(req: Request) -> Response:
        token = req.param("token")
        link = await find(token)
        if req.user is not None:
            return await join(req, link)
        form = await req.form()
        name = form.get("name", "")
        ctx = {"link": link, "token": token, "guest_name": name, "guests": app.policy("guests")}
        if not app.policy("guests"):
            return app.page(req, "share.html", 403, error="Guest access is turned off. Log in to open this project.",
                            **ctx)
        if not app.throttle.per_ip.allow("share:" + req.client_ip):
            return app.page(req, "share.html", 429, error="Too many attempts. Wait a few minutes and try again.",
                            **ctx)
        if link["password_hash"]:
            ok = await asyncio.to_thread(passwords.verify_password, form.get("password", ""), link["password_hash"])
            if not ok:
                return app.page(req, "share.html", 400, error="The password is wrong.", **ctx)
        idle = cfg.security.guest_session_hours * 3600
        old = req.session

        def work(c):
            guest_id = links.create_guest(c, link, name)
            if old is not None:
                sessions.delete(c, old)
            tok, _ = sessions.create(c, None, idle, req.headers.get("user-agent", ""), guest_id=guest_id)
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'guest.join', ?, ?)",
                      (f"guest:{guest_id}", link["project_id"], json.dumps({"link": link["id"]})))
            return tok

        try:
            session_token = await app.db.write(work)
        except ValueError as exc:
            return app.page(req, "share.html", 400, error=str(exc), **ctx)
        resp = redirect(await start_url(link["project_id"], link["review"]))
        resp.set_cookie(cfg.cookie_name, session_token, max_age=idle, secure=cfg.secure_cookies)
        return resp

    app.router.add("GET", "/s/{token}", landing, auth=False)
    app.router.add("POST", "/s/{token}", enter, anonymous=True)
