"""Self-service for an account (§13.7): its sessions, its trash, a copy of its data and deleting it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from ..api.offline import _safe_name, project_files
from ..auth import sessions, users
from ..http.request import HTTPError, Request
from ..http.response import Response, redirect
from ..projects import service as svc

log = logging.getLogger("docwizard.accounts")

RECENT_LOGIN_SECONDS = 15 * 60  # deleting an account without a password needs a login this recent
EXPORT_KEEP_SECONDS = 3600

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"),
             ("CriOS/", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
            ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def device_label(user_agent: str) -> str:
    """A short name for a session's browser, such as "Firefox on Linux"."""
    ua = user_agent or ""
    if ua.startswith("DocWizard") or not ua:
        return "Unknown device"
    browser = next((name for key, name in _BROWSERS if key in ua), "")
    system = next((name for key, name in _SYSTEMS if key in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or ua[:40]


def register(app) -> None:
    cfg = app.cfg
    exporting: set[int] = set()

    # -- the settings page's sections ----------------------------------------------
    async def settings_extras(req: Request, ctx: dict) -> None:
        uid = req.user.id
        current = req.session.token_hash.hex()[:16]
        rows = await app.db.read(lambda c: sessions.list_for_user(c, uid))
        ctx["sessions"] = [{**r, "label": device_label(r["user_agent"]), "current": r["id"] == current}
                           for r in rows]
        ctx["trashed_projects"] = await app.db.read(lambda c: svc.trashed_projects(c, uid))
        ctx.setdefault("delete_error", None)

    app.settings_extras = settings_extras

    # -- sessions --------------------------------------------------------------------
    async def revoke(req: Request) -> Response:
        form = await req.form()
        uid = req.user.id
        keep = req.session.token_hash
        if form.get("all_others") == "1":
            await app.db.write(lambda c: sessions.delete_for_user(c, uid, keep=keep))
            await audit(uid, "session.revoke_others")
            return redirect("/settings?saved=sessions#sessions")
        sid = form.get("id", "")
        if sid == keep.hex()[:16]:
            # Revoking this session is logging out.
            await app.db.write(lambda c: sessions.delete(c, req.session))
            resp = redirect("/login")
            resp.delete_cookie(cfg.cookie_name, secure=cfg.secure_cookies)
            return resp
        await app.db.write(lambda c: sessions.delete_by_prefix(c, uid, sid))
        await audit(uid, "session.revoke")
        return redirect("/settings?saved=sessions#sessions")

    async def audit(uid: int, action: str, target: str | None = None, **meta) -> None:
        await app.db.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, ?, ?, ?)",
                             (f"user:{uid}", action, target, json.dumps(meta)))

    # -- trash -----------------------------------------------------------------------
    async def purge(pids: list[str]) -> None:
        """Delete projects for good, with their folders and published sites."""
        for pid in pids:
            doc_ids = [r["id"] for r in await app.db.fetchall("SELECT id FROM documents WHERE project_id = ?", (pid,))]
            for doc_id in doc_ids:
                await app.rooms.close_document(doc_id)
            slugs = await app.db.write(lambda c, pid=pid: svc.purge_project(c, pid))
            for slug in slugs:
                await asyncio.to_thread(app.publisher.remove_site, slug)
            await asyncio.to_thread(shutil.rmtree, app.rooms.project_dir(pid), True)

    app.purge_projects = purge

    async def trash(req: Request) -> Response:
        form = await req.form()
        uid = req.user.id
        action = form.get("action", "")
        trashed = {p["id"] for p in await app.db.read(lambda c: svc.trashed_projects(c, uid))}
        pid = form.get("project_id", "")
        if action == "restore-project" and pid in trashed:
            await app.db.execute("UPDATE projects SET deleted_at = NULL WHERE id = ? AND owner_id = ?", (pid, uid))
            await audit(uid, "project.restore", pid)
        elif action == "purge-project" and pid in trashed:
            await purge([pid])
            await audit(uid, "project.purge", pid)
        elif action == "empty-all":
            # Every trashed project, and the trash of every project this account owns.
            await purge(sorted(trashed))
            owned = [r["id"] for r in await app.db.fetchall(
                "SELECT id FROM projects WHERE owner_id = ? AND deleted_at IS NULL", (uid,))]
            removed = {"projects": len(trashed), "documents": 0, "assets": 0}
            for p in owned:
                result = await app.db.write(lambda c, p=p: svc.empty_trash(c, p))
                removed["documents"] += result["documents"]
                removed["assets"] += result["assets"]
            await audit(uid, "trash.empty_all", None, **removed)
        else:
            raise HTTPError(400, "bad_request", "Unknown action.")
        app.quotas.forget(uid)
        return redirect("/settings?saved=trash#storage")

    # -- export ----------------------------------------------------------------------
    async def export(req: Request) -> Response:
        """A zip of everything the account holds: its projects as plain files, and the rest as JSON."""
        user = req.user
        if user.id in exporting:
            raise HTTPError(429, "busy", "An export is already being made. Wait a moment and try again.")
        exporting.add(user.id)
        try:
            return await build_export(user)
        finally:
            exporting.discard(user.id)

    async def build_export(user) -> Response:
        uid = user.id

        def load(c):
            account = dict(c.execute("SELECT username, display_name, email, email_verified_at, instance_role,"
                                     " preferences, quota_bytes, created_at FROM users WHERE id = ?",
                                     (uid,)).fetchone())
            projects = [dict(r) for r in c.execute(
                "SELECT p.id, p.name, p.layout_id, p.created_at, p.updated_at, m.role FROM projects p"
                " JOIN project_members m ON m.project_id = p.id AND m.user_id = ? WHERE p.deleted_at IS NULL"
                " ORDER BY p.name COLLATE NOCASE", (uid,))]
            comments = [dict(r) for r in c.execute(
                "SELECT d.project_id, d.path, cm.body, cm.quote, cm.suggestion, cm.created_at FROM comments cm"
                " JOIN documents d ON d.id = cm.doc_id WHERE cm.author_user_id = ? ORDER BY cm.id", (uid,))]
            identities = [dict(r) for r in c.execute("SELECT provider, email, last_login_at FROM identities"
                                                     " WHERE user_id = ?", (uid,))]
            libraries = [dict(r) for r in c.execute("SELECT name, kind, library_type, library_id, collection"
                                                    " FROM citation_sources WHERE owner_id = ?", (uid,))]
            layouts = [json.loads(r["manifest_json"]) for r in c.execute(
                "SELECT manifest_json FROM custom_layouts WHERE owner_id = ?", (uid,))]
            versions = [dict(r) for r in c.execute(
                "SELECT d.project_id, d.path, s.label, s.created_at, s.text FROM doc_snapshots s"
                " JOIN documents d ON d.id = s.doc_id AND d.deleted_at IS NULL"
                " JOIN projects p ON p.id = d.project_id AND p.owner_id = ? AND p.deleted_at IS NULL"
                " WHERE s.label IS NOT NULL ORDER BY s.id", (uid,))]
            return account, projects, comments, identities, libraries, layouts, versions

        account, projects, comments, identities, libraries, layouts, versions = await app.db.read(load)
        try:
            account["preferences"] = json.loads(account["preferences"] or "{}")
        except ValueError:
            pass
        folders = {}
        used = set()
        for p in projects:
            name = _safe_name(p["name"])
            folder = name if name not in used else f"{name} ({p['id']})"
            used.add(folder)
            folders[p["id"]] = ("projects/" if p["role"] == "owner" else "shared/") + folder
            p["folder"] = folders[p["id"]]
        meta = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "server": cfg.server.public_url,
                "account": account, "projects": projects, "comments": comments, "logins": identities,
                "citation_libraries": libraries, "custom_layouts": layouts}
        out_dir = cfg.cache_dir / "exports"
        await asyncio.to_thread(_prune_exports, out_dir)
        target = out_dir / f"{secrets.token_hex(12)}.zip"
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("account.json", json.dumps(meta, ensure_ascii=False, indent=2))
            for v in versions:
                folder = folders.get(v["project_id"])
                if folder:
                    stamp = re.sub(r"[^0-9T]", "", v["created_at"] or "")[:15]
                    label = _safe_name(v["label"] or "version")[:40]
                    zf.writestr(f"versions/{folder.split('/', 1)[1]}/{v['path']}.{stamp} {label}.txt", v["text"])
            for p in projects:
                for path, data in await project_files(app, p["id"]):
                    await asyncio.to_thread(zf.writestr, f"{folders[p['id']]}/{path}", data)
        await audit(uid, "account.export")
        resp = Response(b"", 200, "application/zip")
        resp.file_path = target
        name = f"docwizard-{user.username}-{time.strftime('%Y%m%d')}.zip"
        resp.set_header("content-disposition", f'attachment; filename="{name}"')
        resp.set_header("cache-control", "no-store")
        return resp

    # -- deleting the account ------------------------------------------------------------
    async def delete_account(req: Request) -> Response:
        form = await req.form()
        user = req.user
        uid = user.id

        async def fail(message: str) -> Response:
            return await app.render_settings(req, 400, delete_error=message)

        if form.get("confirm_username", "").strip().lower() != user.username.lower():
            return await fail("Type your username to confirm.")
        row = await app.db.read(lambda c: users.get_by_username(c, user.username))
        if row["password_hash"] is not None:
            if not await asyncio.to_thread(users.authenticate, row, form.get("password", "")):
                return await fail("The current password is wrong.")
        else:
            created = await app.db.fetchone("SELECT created_at FROM sessions WHERE token_hash = ?",
                                            (req.session.token_hash,))
            if created is None or time.time() - created["created_at"] > RECENT_LOGIN_SECONDS:
                return await fail("Log out and in again, then delete the account within 15 minutes.")
        if user.totp and not await app.check_totp_code(uid, form.get("code", "").strip()):
            return await fail("The two-step login code is wrong.")
        if user.is_admin:
            admins = (await app.db.fetchone("SELECT COUNT(*) FROM users WHERE instance_role = 'admin' AND active = 1"
                                            " AND id != ?", (uid,)))[0]
            if not admins:
                return await fail("You are the only admin. Make someone else an admin first.")
        hand_over = form.get("hand_over") == "1"

        def plan(c):
            owned = c.execute("SELECT id, deleted_at FROM projects WHERE owner_id = ?", (uid,)).fetchall()
            transfer, remove = [], []
            for p in owned:
                heir = None
                if hand_over and p["deleted_at"] is None:
                    heir = c.execute("SELECT m.user_id FROM project_members m JOIN users u ON u.id = m.user_id"
                                     " WHERE m.project_id = ? AND m.role = 'editor' AND u.active = 1"
                                     " ORDER BY m.user_id LIMIT 1", (p["id"],)).fetchone()
                if heir is not None:
                    transfer.append((p["id"], heir[0]))
                else:
                    remove.append(p["id"])
            return transfer, remove

        transfer, remove = await app.db.read(plan)
        await purge(remove)

        def work(c):
            for pid, heir in transfer:
                c.execute("UPDATE projects SET owner_id = ? WHERE id = ?", (heir, pid))
                c.execute("UPDATE project_members SET role = 'owner' WHERE project_id = ? AND user_id = ?", (pid, heir))
            # Nothing personal stays in the audit log: entries about this account lose their details.
            c.execute("UPDATE audit_log SET meta_json = '{}' WHERE target = ? OR actor = ?",
                      (f"user:{uid}", f"user:{uid}"))
            for table in ("identities", "sessions", "account_tokens", "recovery_codes", "notifications"):
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM citation_sources WHERE owner_id = ?", (uid,))
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'account.delete', ?, ?)",
                      (f"user:{uid}", f"user:{uid}",
                       json.dumps({"projects_deleted": len(remove), "projects_handed_over": len(transfer)})))

        await app.db.write(work)
        app.quotas.forget(uid)
        log.info("account deleted", extra={"user": uid})
        resp = redirect("/login?deleted=1")
        resp.delete_cookie(cfg.cookie_name, secure=cfg.secure_cookies)
        return resp

    r = app.router
    r.add("POST", "/settings/sessions/revoke", revoke, totp_exempt=True)
    r.add("POST", "/settings/trash", trash)
    r.add("GET", "/settings/export", export)
    r.add("POST", "/settings/delete-account", delete_account, totp_exempt=True)


def _prune_exports(folder: Path) -> None:
    """Exports are files so big accounts don't fill memory; they go after an hour."""
    if not folder.is_dir():
        return
    cutoff = time.time() - EXPORT_KEEP_SECONDS
    for entry in os.scandir(folder):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue
