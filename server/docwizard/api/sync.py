"""JSON API: a project's sync target (§12), including Nextcloud Login Flow v2."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from urllib.parse import urlsplit

from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response
from ..netpolicy import FetchError, PolicyError
from ..secretbox import context_for
from ..sync import nextcloud
from ..sync.engine import KIND_LABELS
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
            "lastError": t["last_error"], "lastSyncAt": t["last_sync_at"], "pollSeconds": t["poll_seconds"]}


def register(app) -> None:
    r = app.router
    flows: dict[str, dict] = {}  # flow id -> {flow, server, project, user, remote_path, expires}

    async def get_target(project_id: str):
        return await app.db.fetchone("SELECT * FROM sync_targets WHERE project_id = ?", (project_id,))

    async def save_target(req: Request, project_id: str, kind: str, base_url: str, remote_path: str,
                          username: str, password: str) -> dict:
        """Check the connection, then store the target with its password sealed."""
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
            c.execute("DELETE FROM sync_targets WHERE project_id = ?", (project_id,))
            tid = c.execute(
                "INSERT INTO sync_targets(project_id, kind, base_url, remote_path, username, poll_seconds, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, kind, base_url, remote_path, username, app.cfg.sync.default_poll_seconds,
                 req.user.id)).lastrowid
            c.execute("UPDATE sync_targets SET secret_enc = ? WHERE id = ?",
                      (app.secrets.seal(password, context_for("sync_targets", tid)), tid))
            c.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES (?, 'sync.connect', ?, ?)",
                      (f"user:{req.user.id}", project_id, json.dumps({"kind": kind, "url": base_url})))
            return tid

        await app.db.write(work)
        return _public(await get_target(project_id))

    async def show(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        return json_response({"target": _public(await get_target(project["id"]))})

    async def connect(req: Request) -> Response:
        """Generic WebDAV and Seafile: URL, folder, username and password or token."""
        project, _ = await project_access(app, req, req.param("id"), "sync")
        body = await req.json_object()
        kind = body.get("kind", "webdav")
        if kind not in ("webdav", "seafile"):
            raise bad_request("Use the Nextcloud login for Nextcloud.")
        url = str(body.get("url", "")).strip().rstrip("/")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise bad_request("Enter the WebDAV address, starting with https://.")
        target = await save_target(req, project["id"], kind, url, _remote_path(body.get("remotePath")),
                                   str(body.get("username", "")), str(body.get("password", "")))
        app.sync.nudge(project["id"])
        return json_response({"target": target}, 201)

    async def disconnect(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "sync")
        await app.db.execute("DELETE FROM sync_targets WHERE project_id = ?", (project["id"],))
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
