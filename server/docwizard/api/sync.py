"""JSON API: a project's sync target (§12), including Nextcloud Login Flow v2, a folder on
the server (§12.6) and a git repository (§12.7)."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response
from ..netpolicy import FetchError, PolicyError
from ..secretbox import context_for
from ..sync import git as gitsync
from ..sync import nextcloud
from ..sync.engine import KIND_LABELS, git_workdir
from ..sync.folder import allowed_root
from ..sync.webdav import DavError, WebDav
from .projects import project_access

FLOW_SECONDS = 20 * 60


def _remote_path(value) -> str:
    path = str(value or "").strip().strip("/")
    if any(part in ("", ".", "..") for part in path.split("/")) and path:
        raise bad_request("The remote folder can't contain empty parts, '.' or '..'.")
    if len(path) > 512:
        raise bad_request("The remote folder name is too long.")
    return path


def _public(t) -> dict | None:
    if t is None:
        return None
    return {"kind": t["kind"], "label": KIND_LABELS.get(t["kind"], "WebDAV"), "url": t["base_url"],
            "remotePath": t["remote_path"], "username": t["username"], "status": t["status"],
            "branch": t["branch"] or None, "author": t["author_name"] or None,
            "lastError": t["last_error"], "lastSyncAt": t["last_sync_at"], "pollSeconds": t["poll_seconds"]}


def options(app) -> dict:
    """The kinds of sync this server offers besides WebDAV."""
    return {"folder": bool(app.cfg.sync.folder_roots), "folderRoots": list(app.cfg.sync.folder_roots),
            "git": app.cfg.sync.git and gitsync.available()}


def register(app) -> None:
    r = app.router
    flows: dict[str, dict] = {}  # flow id -> {flow, server, project, user, remote_path, expires}

    async def get_target(project_id: str):
        return await app.db.fetchone("SELECT * FROM sync_targets WHERE project_id = ?", (project_id,))

    async def save_target(req: Request, project_id: str, kind: str, base_url: str, remote_path: str,
                          username: str, password: str, *, branch: str = "", author: tuple[str, str] = ("", ""),
                          checked: bool = False) -> dict:
        """Check the connection, then store the target with its password sealed."""
        if not checked:
            client = WebDav(app.net, base_url, username, password, timeout=15)
            try:
                await asyncio.to_thread(client.propfind, "")
            except DavError as exc:
                if exc.status in (401, 403):
                    raise bad_request("The server didn't accept these credentials.") from None
                raise bad_request(f"The server didn't answer like a WebDAV server: {exc}") from None
            except (FetchError, PolicyError) as exc:
                raise bad_request(str(exc)) from None

        def work(c):
            old = c.execute("SELECT id, kind FROM sync_targets WHERE project_id = ?", (project_id,)).fetchone()
            c.execute("DELETE FROM sync_targets WHERE project_id = ?", (project_id,))
            tid = c.execute(
                "INSERT INTO sync_targets(project_id, kind, base_url, remote_path, branch, author_name, author_email,"
                " username, poll_seconds, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, kind, base_url, remote_path, branch, author[0], author[1], username,
                 app.cfg.sync.default_poll_seconds, req.user.id)).lastrowid
            if password:
                c.execute("UPDATE sync_targets SET secret_enc = ? WHERE id = ?",
                          (app.secrets.seal(password, context_for("sync_targets", tid)), tid))
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'sync.connect', ?, ?)",
                      (f"user:{req.user.id}", project_id, json.dumps({"kind": kind, "url": base_url})))
            return old

        old = await app.db.write(work)
        if old is not None and old["kind"] == "git":
            await asyncio.to_thread(shutil.rmtree, git_workdir(app, old["id"]), True)
        return _public(await get_target(project_id))

    async def show(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        return json_response({"target": _public(await get_target(project["id"])), "options": options(app)})

    async def connect(req: Request) -> Response:
        """Generic WebDAV and Seafile: URL, folder, username and password or token."""
        project, _ = await project_access(app, req, req.param("id"), "sync")
        body = await req.json_object()
        kind = body.get("kind", "webdav")
        if kind != "folder":
            app.require_verified_email(req, "sync")
        if kind == "folder":
            target = await connect_folder(req, project, body)
            app.sync.nudge(project["id"])
            return json_response({"target": target}, 201)
        if kind == "git":
            target = await connect_git(req, project, body)
            app.sync.nudge(project["id"])
            return json_response({"target": target}, 201)
        if kind not in ("webdav", "seafile"):
            raise bad_request("Use the Nextcloud login for Nextcloud.")
        url = str(body.get("url", "")).strip().rstrip("/")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise bad_request("Enter the WebDAV address, starting with https://.")
        target = await save_target(req, project["id"], kind, url, _remote_path(body.get("remotePath")),
                                   str(body.get("username", "")), str(body.get("password", "")))
        app.sync.nudge(project["id"])
        return json_response({"target": target}, 201)

    async def connect_folder(req: Request, project, body: dict) -> dict:
        """A folder on this server, inside one an admin listed (§12.6)."""
        try:
            root, rel = allowed_root(app.cfg.sync.folder_roots, str(body.get("folder", "")))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        wanted = f"{root}/{rel}/"
        for other in await app.db.fetchall(
                "SELECT project_id, base_url, remote_path FROM sync_targets WHERE kind = 'folder' AND project_id != ?",
                (project["id"],)):
            theirs = f"{other['base_url']}/{other['remote_path']}/"
            if theirs.startswith(wanted) or wanted.startswith(theirs):
                raise bad_request("Another project already syncs with this folder or one around it.")
        target = Path(root, rel)
        try:
            await asyncio.to_thread(lambda: target.mkdir(parents=True, exist_ok=True))
        except OSError as exc:
            raise bad_request(f"The folder can't be created: {exc.strerror}") from None
        return await save_target(req, project["id"], "folder", str(root), rel, "", "", checked=True)

    async def connect_git(req: Request, project, body: dict) -> dict:
        """A branch of a git repository over HTTPS (§12.7)."""
        if not options(app)["git"]:
            raise bad_request("Git sync isn't available on this server.")
        try:
            url = gitsync.check_url(str(body.get("url", "")))
            branch = gitsync.check_branch(str(body.get("branch", "")))
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        username = str(body.get("username", "")).strip()
        token = str(body.get("password", "")).strip()
        name = str(body.get("authorName", "")).strip()[:100] or req.user.display_name
        email = str(body.get("authorEmail", "")).strip()[:200] or (req.user.email or "")
        if any(c in name + email for c in "<>\n\r\0"):
            raise bad_request("The commit author can't contain <, > or line breaks.")
        probe = gitsync.GitRemote(app.net, app.cfg.data_dir / "sync-git" / "probe", url, branch,
                                  username=username, token=token)
        try:
            await asyncio.to_thread(probe.probe)
        except gitsync.GitError as exc:
            raise bad_request(str(exc)) from None
        return await save_target(req, project["id"], "git", url, "", username, token, branch=branch,
                                 author=(name or "DocWizard", email or "docwizard@localhost"), checked=True)

    async def disconnect(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        target = await get_target(project["id"])
        await app.db.execute("DELETE FROM sync_targets WHERE project_id = ?", (project["id"],))
        if target is not None and target["kind"] == "git":
            await asyncio.to_thread(shutil.rmtree, git_workdir(app, target["id"]), True)
        return json_response({"ok": True})

    async def patch(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        body = await req.json_object()
        if "paused" in body:
            status = "paused" if body["paused"] else "new"
            await app.db.execute("UPDATE sync_targets SET status = ?, next_poll_at = 0 WHERE project_id = ?",
                                 (status, project["id"]))
        return json_response({"target": _public(await get_target(project["id"]))})

    async def run_now(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        target = await get_target(project["id"])
        if target is None:
            raise not_found("This project isn't synced.")
        report = await app.sync.sync_target(target["id"])
        return json_response({"report": report, "target": _public(await get_target(project["id"]))})

    # -- Nextcloud Login Flow v2 ---------------------------------------------
    async def nextcloud_start(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        app.require_verified_email(req, "sync")
        body = await req.json_object()
        try:
            server = nextcloud.normalize_server(str(body.get("server", "")))
            flow = await asyncio.to_thread(nextcloud.start, app.net, server)
        except (nextcloud.FlowError, FetchError, PolicyError) as exc:
            raise bad_request(str(exc)) from None
        now = time.time()
        for key in [k for k, v in flows.items() if v["expires"] < now]:
            del flows[key]
        flow_id = secrets.token_urlsafe(16)
        flows[flow_id] = {"flow": flow, "server": server, "project": project["id"], "user": req.user.id,
                          "remote_path": _remote_path(body.get("remotePath")), "expires": now + FLOW_SECONDS}
        return json_response({"flow": flow_id, "login": flow.login_url}, 201)

    async def nextcloud_poll(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        state = flows.get(req.param("flow"))
        if state is None or state["project"] != project["id"] or state["user"] != req.user.id \
                or state["expires"] < time.time():
            raise HTTPError(404, "flow_expired", "The Nextcloud login expired. Start again.")
        try:
            creds = await asyncio.to_thread(nextcloud.poll, app.net, state["flow"])
        except (nextcloud.FlowError, FetchError, PolicyError) as exc:
            raise bad_request(str(exc)) from None
        if creds is None:
            return json_response({"done": False})
        flows.pop(req.param("flow"), None)
        # The server we started with, not the one in the answer.
        target = await save_target(req, project["id"], "nextcloud",
                                   nextcloud.dav_root(state["server"], creds.login_name), state["remote_path"],
                                   creds.login_name, creds.app_password)
        app.sync.nudge(project["id"])
        return json_response({"done": True, "target": target})

    r.add("GET", "/api/projects/{id}/sync", show)
    r.add("PUT", "/api/projects/{id}/sync", connect)
    r.add("PATCH", "/api/projects/{id}/sync", patch)
    r.add("DELETE", "/api/projects/{id}/sync", disconnect)
    r.add("POST", "/api/projects/{id}/sync/run", run_now)
    r.add("POST", "/api/projects/{id}/sync/nextcloud", nextcloud_start)
    r.add("GET", "/api/projects/{id}/sync/nextcloud/{flow}", nextcloud_poll)
