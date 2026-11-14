"""The admin area (§13.6): users, instance settings, layouts, health and the audit log."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from ..auth import sessions, users
from ..http.request import HTTPError, Request
from ..http.response import Response, redirect
from ..mail import MailError

ROLES = ("viewer", "commenter", "editor")
REGISTRATION = ("open", "invite", "oidc-only", "closed")
HOST_OR_NET = re.compile(r"^[A-Za-z0-9.:\[\]-]+(/[0-9]{1,3})?$")


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def register(app) -> None:
    r = app.router

    def require_admin(req: Request) -> None:
        if req.user is None or not req.user.is_admin:
            raise HTTPError(404, "not_found", "This page doesn't exist.")

    async def audit(req: Request, action: str, target: str | None = None, **meta) -> None:
        await app.db.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, ?, ?, ?)",
                             (f"user:{req.user.id}", action, target, json.dumps(meta)))

    def page(req: Request, tab: str, status: int = 200, **ctx) -> Response:
        return app.page(req, f"admin_{tab}.html", status, tab=tab, **ctx)

    # -- health -------------------------------------------------------------
    async def health(req: Request) -> Response:
        require_admin(req)
        worker_ok = True
        try:
            versions = await asyncio.wait_for(app.renderer.worker.hello(), 5)
        except Exception:  # noqa: BLE001
            worker_ok, versions = False, app.renderer.worker.versions

        def disk():
            out = []
            for label, path in (("Data", app.cfg.data_dir), ("Render jobs", app.cfg.jobs_dir),
                                ("Cache", app.cfg.cache_dir), ("Published sites", app.publisher.root)):
                if not Path(path).exists():
                    continue
                usage = shutil.disk_usage(path)
                out.append({"label": label, "path": str(path), "used": _dir_size(Path(path)), "free": usage.free})
            return out

        counts = {}
        for key, sql in (("users", "SELECT COUNT(*) FROM users WHERE active = 1"),
                         ("projects", "SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL"),
                         ("documents", "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"),
                         ("sessions", "SELECT COUNT(*) FROM sessions WHERE expires_at > strftime('%s','now')"),
                         ("sync", "SELECT COUNT(*) FROM sync_targets"),
                         ("sync_errors", "SELECT COUNT(*) FROM sync_targets WHERE status = 'error'"),
                         ("publications", "SELECT COUNT(*) FROM publications")):
            counts[key] = (await app.db.fetchone(sql))[0]
        return page(req, "health", worker_ok=worker_ok, versions=versions, disk=await asyncio.to_thread(disk),
                    counts=counts, rooms=len(app.rooms.rooms) if hasattr(app.rooms, "rooms") else 0,
                    inflight=len(app.renderer._inflight), errors=list(reversed(app.recent_errors)),
                    release=app.cfg.release, fts=app.search.available if hasattr(app, "search") else False)

    # -- users ----------------------------------------------------------------
    async def users_page(req: Request, status: int = 200, **ctx) -> Response:
        require_admin(req)
        q = req.query.get("q", "").strip()
        sql = ("SELECT u.*, (SELECT COUNT(*) FROM projects p WHERE p.owner_id = u.id AND p.deleted_at IS NULL) AS owned,"
               " (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.expires_at > ?) AS live_sessions,"
               " (SELECT MAX(at) FROM audit_log a WHERE a.actor = 'user:' || u.id AND a.action = 'login') AS last_login"
               " FROM users u")
        params: list = [time.time()]
        if q:
            sql += " WHERE u.username LIKE ? OR u.display_name LIKE ? OR u.email LIKE ?"
            params += [f"%{q}%"] * 3
        rows = await app.db.fetchall(sql + " ORDER BY u.username COLLATE NOCASE LIMIT 500", tuple(params))
        ctx.setdefault("error", None)
        ctx.setdefault("link", None)
        ctx.setdefault("notice", req.query.get("done"))
        return page(req, "users", status, users=[dict(r) for r in rows], q=q, mail=app.mail.enabled, **ctx)

    async def users_get(req: Request) -> Response:
        return await users_page(req)

    async def users_create(req: Request) -> Response:
        require_admin(req)
        form = await req.form()
        username = form.get("username", "").strip()
        display_name = form.get("display_name", "").strip()[:80]
        email = form.get("email", "").strip()[:200] or None
        role = "admin" if form.get("role") == "admin" else "user"
        if email and "@" not in email:
            return await users_page(req, 400, error="This email address doesn't look right.")
        try:
            uid = await app.db.write(lambda c: users.create(c, username, None, display_name=display_name,
                                                             email=email, role=role))
        except ValueError as exc:
            return await users_page(req, 400, error=str(exc))
        await audit(req, "user.create", f"user:{uid}", username=username, role=role)
        return await hand_out(req, uid, username, "invite")

    async def hand_out(req: Request, uid: int, username: str, purpose: str) -> Response:
        """Email the link when possible, otherwise show it to the admin."""
        if app.mail.enabled:
            try:
                await app.mail_account_link(uid, purpose, req.user.id)
                return redirect("/admin/users?done=" + ("invited" if purpose == "invite" else "reset-sent"))
            except MailError as exc:
                url = await app.issue_account_link(uid, purpose, req.user.id)
                return await users_page(req, link={"username": username, "url": url, "purpose": purpose,
                                                   "why": str(exc)})
        url = await app.issue_account_link(uid, purpose, req.user.id)
        return await users_page(req, link={"username": username, "url": url, "purpose": purpose, "why": None})

    async def user_action(req: Request) -> Response:
        require_admin(req)
        form = await req.form()
        try:
            uid = int(req.param("id"))
        except ValueError:
            raise HTTPError(404, "not_found") from None
        row = await app.db.fetchone("SELECT * FROM users WHERE id = ?", (uid,))
        if row is None:
            raise HTTPError(404, "not_found", "No such user.")
        action = form.get("action", "")
        me = uid == req.user.id
        if me and action in ("disable", "make-user", "delete"):
            return await users_page(req, 400, error="You can't do this to your own account.")
        if action in ("disable", "enable"):
            active = int(action == "enable")

            def work(c):
                c.execute("UPDATE users SET active = ? WHERE id = ?", (active, uid))
                if not active:
                    sessions.delete_for_user(c, uid)

            await app.db.write(work)
        elif action in ("make-admin", "make-user"):
            await app.db.execute("UPDATE users SET instance_role = ? WHERE id = ?",
                                 ("admin" if action == "make-admin" else "user", uid))
        elif action in ("require-totp", "unrequire-totp"):
            await app.db.execute("UPDATE users SET require_totp = ? WHERE id = ?", (int(action == "require-totp"), uid))
        elif action == "reset-totp":
            def off(c):
                c.execute("UPDATE users SET totp_secret_enc = NULL, totp_last_counter = NULL WHERE id = ?", (uid,))
                c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (uid,))

            await app.db.write(off)
        elif action == "logout":
            await app.db.write(lambda c: sessions.delete_for_user(c, uid))
        elif action in ("reset-link", "invite"):
            await audit(req, f"user.{action}", f"user:{uid}")
            return await hand_out(req, uid, row["username"], "invite" if action == "invite" else "reset")
        elif action == "update":
            email = form.get("email", "").strip()[:200] or None
            quota = form.get("quota_gb", "").strip()
            if email and "@" not in email:
                return await users_page(req, 400, error="This email address doesn't look right.")
            try:
                quota_bytes = int(float(quota) * 1_000_000_000) if quota else None
            except ValueError:
                return await users_page(req, 400, error="The quota must be a number of gigabytes.")
            await app.db.execute("UPDATE users SET email = ?, quota_bytes = ? WHERE id = ?", (email, quota_bytes, uid))
        elif action == "delete":
            owned = (await app.db.fetchone("SELECT COUNT(*) FROM projects WHERE owner_id = ?", (uid,)))[0]
            if owned:
                return await users_page(req, 400, error=app.t(
                    req, "{username} owns 1 project (trashed ones included). Delete it or wait until the trash is"
                    " emptied, then try again." if owned == 1 else "{username} owns {count} projects (trashed ones"
                    " included). Delete them or wait until the trash is emptied, then try again.",
                    username=row["username"], count=owned))
            try:
                await app.db.execute("DELETE FROM users WHERE id = ?", (uid,))
            except sqlite3.IntegrityError:
                return await users_page(req, 400, error="This account is still referenced and can't be deleted.")
        else:
            raise HTTPError(400, "bad_request", "Unknown action.")
        await audit(req, f"user.{action}", f"user:{uid}", username=row["username"])
        return redirect("/admin/users?done=" + action + ("&q=" + form.get("q", "") if form.get("q") else ""))

    # -- settings ---------------------------------------------------------------
    async def settings_page(req: Request, status: int = 200, **ctx) -> Response:
        require_admin(req)
        values = {k: app.policy(k) for k in app.POLICY_KEYS}
        overridden = set(app.settings)
        oidc = [{"id": p.id, "label": p.label, "issuer": p.issuer} for p in app.oidc.list()]
        ctx.setdefault("error", None)
        ctx.setdefault("notice", req.query.get("done"))
        ctx.setdefault("checks", {})
        smtp = app.cfg.smtp
        return page(req, "settings", status, values=values, overridden=overridden, roles=ROLES,
                    registrations=REGISTRATION, layouts=app.layouts.list(), oidc=oidc,
                    smtp={"enabled": app.mail.enabled, "host": smtp.host, "port": smtp.port, "sender": app.mail.sender()},
                    **ctx)

    async def settings_get(req: Request) -> Response:
        return await settings_page(req)

    async def settings_post(req: Request) -> Response:
        require_admin(req)
        form = await req.form()
        action = form.get("action", "save")
        if action == "test-mail":
            to = form.get("to", "").strip() or req.user.email or ""
            if "@" not in to:
                return await settings_page(req, 400, error="Enter an address to send the test message to.")
            try:
                await app.mail.send(to, "DocWizard test message", "Mail from DocWizard works.\n")
            except MailError as exc:
                return await settings_page(req, 400, error=str(exc))
            return redirect("/admin/settings?done=mail")
        if action == "check-oidc":
            checks = {}
            for p in app.oidc.list():
                app.oidc._discovery.pop(p.id, None)
                try:
                    await app.oidc.discovery(p)
                    checks[p.id] = None
                except Exception as exc:  # noqa: BLE001
                    checks[p.id] = str(exc) or "The provider couldn't be reached."
            return await settings_page(req, checks=checks)
        new = {}
        reg = form.get("registration", "")
        if reg not in REGISTRATION:
            return await settings_page(req, 400, error="Choose a registration mode.")
        new["registration"] = reg
        new["guests"] = form.get("guests") == "1"
        if form.get("guest_max_role") not in ROLES:
            return await settings_page(req, 400, error="Choose the highest role for guests.")
        new["guest_max_role"] = form["guest_max_role"]
        new["scratch_projects"] = form.get("scratch_projects") == "1"
        new["require_totp_for_admins"] = form.get("require_totp_for_admins") == "1"
        try:
            new["min_password_length"] = max(8, min(128, int(form.get("min_password_length", "12"))))
        except ValueError:
            return await settings_page(req, 400, error="The minimum password length must be a number.")
        if not app.layouts.exists(form.get("default_layout", "")):
            return await settings_page(req, 400, error="Choose a default layout.")
        new["default_layout"] = form["default_layout"]
        allow = [line.strip() for line in form.get("allow_private_outbound", "").splitlines() if line.strip()]
        bad = [a for a in allow if not HOST_OR_NET.match(a)]
        if bad:
            return await settings_page(req, 400, error=app.t(req, "Outbound requests: {value} isn't a host name, address or network.", value=bad[0]))
        new["allow_private_outbound"] = allow
        if new["require_totp_for_admins"] and not req.user.totp:
            return await settings_page(req, 400, error="Turn on two-step login for your own account first, or you "
                                                       "would lock yourself out of the admin area.")
        changed = {k: v for k, v in new.items() if app.policy(k) != v}
        for key, value in changed.items():
            await app.set_setting(key, value)
        if changed:
            await audit(req, "settings.change", None, **changed)
        return redirect("/admin/settings?done=saved")

    # -- layouts ----------------------------------------------------------------
    async def layouts_get(req: Request) -> Response:
        require_admin(req)
        rows = []
        for layout in app.layouts.list():
            files = layout.filter_files()
            rows.append({"id": layout.id, "name": layout.name, "description": layout.description,
                         "version": layout.manifest.get("version", ""), "targets": sorted(layout.targets),
                         "filters": files, "hash": layout.filters_hash() if files else None,
                         "approved": app.layouts.filters_approved(layout),
                         "default": layout.id == app.policy("default_layout")})
        return page(req, "layouts", layouts=rows, notice=req.query.get("done"))

    async def layouts_post(req: Request) -> Response:
        require_admin(req)
        form = await req.form()
        action = form.get("action", "")
        if action == "reload":
            await asyncio.to_thread(app.layouts.reload)
            return redirect("/admin/layouts?done=reloaded")
        if not app.layouts.exists(form.get("layout", "")):
            raise HTTPError(404, "not_found", "No such layout.")
        layout = app.layouts.get(form["layout"])
        digest = layout.filters_hash()
        if action == "approve":
            # The hash shown on the page must still be the current one.
            if form.get("hash") != digest:
                return redirect("/admin/layouts?done=changed")
            app.layouts.approved_filters.add(digest)
        elif action == "revoke":
            app.layouts.approved_filters.discard(digest)
        else:
            raise HTTPError(400, "bad_request", "Unknown action.")
        await app.save_filter_approvals()
        await audit(req, f"layout.{action}", layout.id, hash=digest)
        return redirect("/admin/layouts?done=" + action)

    # -- audit log --------------------------------------------------------------
    async def audit_get(req: Request) -> Response:
        require_admin(req)
        action = req.query.get("action", "").strip()
        actor = req.query.get("actor", "").strip()
        before = req.query.get("before", "")
        sql = "SELECT * FROM audit_log WHERE 1 = 1"
        params: list = []
        if action:
            sql += " AND action LIKE ?"
            params.append(action.rstrip("*") + "%")
        if actor:
            row = await app.db.fetchone("SELECT id FROM users WHERE username = ?", (actor,))
            sql += " AND actor = ?"
            params.append(f"user:{row['id']}" if row else actor)
        if before.isdigit():
            sql += " AND id < ?"
            params.append(int(before))
        rows = await app.db.fetchall(sql + " ORDER BY id DESC LIMIT 101", tuple(params))
        more = len(rows) > 100
        rows = rows[:100]
        ids = {int(r["actor"][5:]) for r in rows if (r["actor"] or "").startswith("user:") and r["actor"][5:].isdigit()}
        names = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for u in await app.db.fetchall(f"SELECT id, username FROM users WHERE id IN ({marks})", tuple(ids)):
                names[f"user:{u['id']}"] = u["username"]
        entries = []
        for r in rows:
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except ValueError:
                meta = {}
            entries.append({"id": r["id"], "at": r["at"], "actor": names.get(r["actor"], r["actor"] or "system"),
                            "action": r["action"], "target": r["target"] or "",
                            "meta": ", ".join(f"{k}: {v}" for k, v in meta.items() if v not in (None, ""))})
        return page(req, "audit", entries=entries, action=action, actor=actor,
                    next_before=entries[-1]["id"] if more and entries else None)

    # -- metrics ----------------------------------------------------------------
    async def metrics(req: Request) -> Response:
        require_admin(req)
        lines = []

        def gauge(name: str, value, help_text: str, labels: str = "") -> None:
            if not any(line.startswith(f"# HELP {name} ") for line in lines):
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{labels} {value}")

        for key, sql, text in (
                ("docwizard_users", "SELECT COUNT(*) FROM users WHERE active = 1", "Active user accounts."),
                ("docwizard_projects", "SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL", "Projects."),
                ("docwizard_documents", "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL", "Documents."),
                ("docwizard_sessions", "SELECT COUNT(*) FROM sessions WHERE expires_at > strftime('%s','now')",
                 "Live sessions."),
                ("docwizard_publications", "SELECT COUNT(*) FROM publications", "Published documents.")):
            gauge(key, (await app.db.fetchone(sql))[0], text)
        for row in await app.db.fetchall("SELECT status, COUNT(*) AS n FROM sync_targets GROUP BY status"):
            gauge("docwizard_sync_targets", row["n"], "Sync targets by status.", f'{{status="{row["status"]}"}}')
        gauge("docwizard_open_documents", len(getattr(app.rooms, "rooms", {})), "Documents loaded in memory.")
        gauge("docwizard_renders_running", len(app.renderer._inflight), "Render jobs queued or running.")
        gauge("docwizard_worker_up", int(bool(app.renderer.worker.versions)), "Whether the render worker answered.")
        gauge("docwizard_recent_errors", len(app.recent_errors), "Errors logged since start (last 50 kept).")
        body = ("\n".join(lines) + "\n").encode()
        return Response(body, 200, "text/plain; version=0.0.4; charset=utf-8")

    r.add("GET", "/admin", health)
    r.add("GET", "/admin/users", users_get)
    r.add("POST", "/admin/users", users_create)
    r.add("POST", "/admin/users/{id}", user_action)
    r.add("GET", "/admin/settings", settings_get)
    r.add("POST", "/admin/settings", settings_post)
    r.add("GET", "/admin/layouts", layouts_get)
    r.add("POST", "/admin/layouts", layouts_post)
    r.add("GET", "/admin/audit", audit_get)
    r.add("GET", "/metrics", metrics)
