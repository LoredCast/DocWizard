"""WebSocket connections and the wire protocol (§8.2).

One socket per browser tab at ``/ws``, JSON text frames multiplexed by
``doc``. The upgrade checks ``Origin`` and the session cookie.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..auth import sessions, users
from ..auth.throttle import TokenBucket
from ..http.request import HTTPError, Request
from ..logs import fields
from ..projects import service as svc
from .authority import Peer, PushError

log = logging.getLogger("docwizard.hub")

MAX_FRAME = 1 << 20
MAX_OPEN_DOCS = 20
MAX_QUEUED = 512


class Connection:
    def __init__(self, hub: Hub, send, user):
        self.hub = hub
        self._send = send
        self.user = user
        self.out: asyncio.Queue = asyncio.Queue()
        self.docs: dict[str, Peer] = {}
        self.rate = TokenBucket(200, 50)
        self.closed = False

    @property
    def key(self) -> int:
        return id(self)

    def send(self, message: dict) -> None:
        if self.closed:
            return
        if self.out.qsize() > MAX_QUEUED:
            # A client that doesn't keep up is disconnected; it reconnects and catches up.
            self.closed = True
            self.out.put_nowait(None)
            return
        self.out.put_nowait(message)

    async def writer(self) -> None:
        while True:
            message = await self.out.get()
            if message is None:
                await self._send({"type": "websocket.close", "code": 1008})
                return
            await self._send({"type": "websocket.send", "text": json.dumps(message, ensure_ascii=False,
                                                                            separators=(",", ":"))})


class Hub:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        req = Request({**scope, "method": "GET"}, receive)
        cfg = self.app.cfg
        origin = req.headers.get("origin", "").lower()
        if scope.get("path") != "/ws" or origin != cfg.app_origin:
            await send({"type": "websocket.close", "code": 1008})
            return
        token = req.cookies.get(cfg.cookie_name, "")
        sess = await self.app.db.read(lambda c: sessions.load(c, token))
        user = None
        if sess is not None and sess.user_id is not None:
            user = await self.app.db.read(lambda c: users.get(c, sess.user_id))
        if user is None:
            await send({"type": "websocket.close", "code": 1008})
            return
        message = await receive()
        if message["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        conn = Connection(self, send, user)
        writer = asyncio.create_task(conn.writer())
        try:
            while not conn.closed:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message["type"] != "websocket.receive":
                    continue
                text = message.get("text")
                if text is None or len(text) > MAX_FRAME:
                    conn.send({"t": "error", "code": "bad_frame", "message": "Frames must be JSON text up to 1 MiB."})
                    continue
                if not conn.rate.allow("msg"):
                    conn.send({"t": "error", "code": "rate_limited", "message": "Too many messages."})
                    conn.closed = True
                    conn.out.put_nowait(None)
                    break
                try:
                    msg = json.loads(text)
                except ValueError:
                    conn.send({"t": "error", "code": "bad_json", "message": "Invalid JSON."})
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    await self.dispatch(conn, msg)
                except (HTTPError, PushError) as exc:
                    conn.send({"t": "error", "code": exc.code, "doc": msg.get("doc"), "message": exc.message})
                except Exception:  # noqa: BLE001
                    log.exception("websocket message failed", extra=fields(t=msg.get("t")))
                    conn.send({"t": "error", "code": "internal", "doc": msg.get("doc"),
                               "message": "Something went wrong on the server."})
        finally:
            renderer = getattr(self.app, "renderer", None)
            if renderer is not None:
                renderer.forget(conn.key)
            for doc_id in list(conn.docs):
                room = self.app.rooms.loaded(doc_id)
                if room is not None:
                    room.leave(conn.key)
            if not conn.closed:
                conn.closed = True
                conn.out.put_nowait(None)
            try:
                await asyncio.wait_for(writer, timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                writer.cancel()

    async def dispatch(self, conn: Connection, msg: dict) -> None:
        kind = msg.get("t")
        doc_id = msg.get("doc")
        if not isinstance(doc_id, str):
            raise HTTPError(400, "bad_request", "Missing document.")
        if kind == "open":
            await self._open(conn, doc_id, msg)
            return
        peer = conn.docs.get(doc_id)
        if peer is None:
            raise HTTPError(400, "not_open", "Open the document first.")
        rooms = self.app.rooms
        if kind == "close":
            conn.docs.pop(doc_id, None)
            room = rooms.loaded(doc_id)
            if room is not None:
                room.leave(conn.key)
            return
        room = await rooms.get(doc_id)
        if kind == "push":
            version = msg.get("version")
            if not isinstance(version, int):
                raise HTTPError(400, "bad_request", "Missing version.")
            ok = await room.call(lambda: room.push(peer, version, msg.get("updates")))
            if not ok:
                conn.send({"t": "rejected", "doc": doc_id, "version": room.version})
        elif kind == "merge":
            merge_id, base, text = msg.get("mergeId"), msg.get("base"), msg.get("text")
            if not (isinstance(merge_id, str) and 8 <= len(merge_id) <= 64 and isinstance(base, str)
                    and isinstance(text, str)):
                raise HTTPError(400, "bad_request", "Invalid merge.")
            reply = await room.call(lambda: room.merge(peer, merge_id, base, text))
            conn.send(reply)
        elif kind == "render":
            renderer = getattr(self.app, "renderer", None)
            if renderer is None:
                raise HTTPError(503, "unavailable", "Rendering is not available.")
            await renderer.request_preview(conn, peer, room, msg)
        else:
            raise HTTPError(400, "bad_request", "Unknown message type.")

    async def _open(self, conn: Connection, doc_id: str, msg: dict) -> None:
        version, client_id = msg.get("version"), msg.get("clientID")
        if not isinstance(version, int) or not isinstance(client_id, str):
            raise HTTPError(400, "bad_request", "Open needs a version and a client ID.")
        if doc_id not in conn.docs and len(conn.docs) >= MAX_OPEN_DOCS:
            raise HTTPError(429, "too_many_documents", "Close a document before opening another.")
        user = conn.user

        def check(c):
            doc = svc.get_document(c, doc_id)
            if doc is None:
                return None, None
            role = svc.role_of(c, user.id, doc["project_id"])
            owner = c.execute("SELECT user_id FROM collab_clients WHERE client_id = ? AND doc_id = ?",
                              (client_id, doc_id)).fetchone()
            return role, owner

        role, owner = await self.app.db.read(check)
        if role is None:
            raise HTTPError(404, "not_found", "This document doesn't exist or you don't have access.")
        # Client IDs are issued by the server and only accepted from their owner (§8.3 rule 5).
        if owner is None or owner["user_id"] != user.id:
            raise HTTPError(403, "bad_client_id", "Reload the page to get a new client ID.")
        await self.app.db.execute("UPDATE collab_clients SET last_seen_at = ? WHERE client_id = ?",
                                  (time.time(), client_id))
        peer = Peer(send=conn.send, user_id=user.id, name=user.display_name, role=role, client_id=client_id)
        conn.docs[doc_id] = peer
        room = await self.app.rooms.get(doc_id)
        await room.call(lambda: room.open(conn.key, peer, version))
