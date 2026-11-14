"""The collaboration authority: one actor per live document (§8.3).

Every room runs one asyncio task that takes commands from its queue and runs
each to completion, including its database commit, before starting the next.
Nothing reads the text while a push's commit is in flight, updates are
broadcast only after the commit, and text and version are reverted together
if anything fails.

The room also owns the document's file: it materializes the text to disk
after activity settles and reconciles changes made to the file from outside
(§11.2, §12.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..auth import permissions
from ..logs import fields
from ..projects import files
from ..projects import service as svc
from .changeset import ChangeSet, InvalidChange, from_diff
from .merge import reconcile
from .text import Utf16Buffer

log = logging.getLogger("docwizard.collab")

MAX_UPDATES_PER_PUSH = 100
RECENT_CHANGES = 2000  # changesets kept in memory to map presence and anchors (§8.7)
MAX_SELECTIONS = 100
PEER_COLORS = 8


class PushError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Peer:
    """What the room needs to know about a connection."""
    send: Callable[[dict], None]
    user_id: int | None
    name: str
    role: str
    client_id: str
    guest_id: int | None = None
    project_id: str | None = None
    # Presence (§8.7): set by the room.
    id: str = ""
    color: int = 0
    sel: list | None = None

    @property
    def identity(self) -> str:
        return f"u{self.user_id}" if self.user_id is not None else f"g{self.guest_id}"

    @property
    def author(self) -> str | None:
        """The value stored as an operation's author."""
        if self.user_id is not None:
            return str(self.user_id)
        if self.guest_id is not None:
            return f"g{self.guest_id}"
        return None

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color, "sel": self.sel}


def map_range(cs: ChangeSet, frm: int, to: int) -> tuple[int, int]:
    """Map an anchored range: text typed at either edge stays outside it (§8.8)."""
    a, b = cs.map_pos(frm, 1), cs.map_pos(to, -1)
    return a, max(a, b)


def map_selection(sel: list, changesets) -> list:
    out = []
    for anchor, head in sel:
        for cs in changesets:
            anchor, head = cs.map_pos(anchor, -1), cs.map_pos(head, -1)
        out.append([anchor, head])
    return out


class Room:
    def __init__(self, rooms: Rooms, doc: dict, state: svc.DocState):
        self.rooms = rooms
        self.doc_id: str = doc["id"]
        self.project_id: str = doc["project_id"]
        self.path: str = doc["path"]
        self.kind: str = doc["kind"]
        self.line_ending: str = doc["line_ending"]
        self.meta = dict(doc)  # materialized_* bookkeeping
        self.buf = Utf16Buffer(state.text)
        self.version = state.version
        self.peers: dict[int, Peer] = {}
        self.recent: deque[tuple[int, ChangeSet]] = deque(maxlen=RECENT_CHANGES)
        # Comment and conflict anchors, kept current through every change (§8.8, §12.3).
        self.anchors: dict[tuple[str, int], list[int]] = {}
        self.anchors_dirty = False
        self._peer_seq = 0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ops_since_snapshot = state.version - doc.get("snapshot_version", 0)
        self.dirty_since: float | None = None
        self.last_op = 0.0
        self.last_activity = time.monotonic()
        self.closed = False
        self.task = asyncio.create_task(self._run(), name=f"room-{self.doc_id}")

    # -- actor ----------------------------------------------------------
    async def _run(self) -> None:
        while True:
            fn, fut = await self.queue.get()
            if fn is None:
                fut.set_result(None)
                return
            try:
                result = await fn()
            except BaseException as exc:  # noqa: BLE001
                if not fut.done():
                    fut.set_exception(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            else:
                if not fut.done():
                    fut.set_result(result)

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        if self.closed:
            raise RuntimeError("room is closed")
        fut = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((fn, fut))
        return await fut

    async def stop(self) -> None:
        if self.closed:
            return
        fut = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((None, fut))
        self.closed = True
        await fut

    @property
    def project_dir(self) -> Path:
        return self.rooms.project_dir(self.project_id)

    @property
    def db(self):
        return self.rooms.app.db

    def text(self) -> str:
        return str(self.buf)

    def broadcast(self, message: dict, exclude: int | None = None) -> None:
        for key, peer in list(self.peers.items()):
            if key != exclude:
                peer.send(message)

    def changes_since(self, version: int) -> list[ChangeSet] | None:
        """Changesets from *version* to the current version, if still in memory."""
        if version == self.version:
            return []
        if version > self.version or not self.recent or self.recent[0][0] > version:
            return None
        return [cs for v, cs in self.recent if v >= version]

    def touch(self) -> None:
        now = time.monotonic()
        self.last_op = now
        self.last_activity = now
        if self.dirty_since is None:
            self.dirty_since = now
        for hook in self.rooms.change_hooks:
            try:
                hook(self)
            except Exception:  # noqa: BLE001
                log.exception("change hook failed")

    # -- commands (run on the actor) -------------------------------------
    def _assign_color(self, peer: Peer, key: int) -> int:
        """A stable color per person; another free one if someone else present has it."""
        preferred = int(hashlib.sha256(peer.identity.encode()).hexdigest(), 16) % PEER_COLORS
        taken = {p.color for k, p in self.peers.items() if k != key and p.identity != peer.identity}
        if preferred not in taken:
            return preferred
        free = [c for c in range(PEER_COLORS) if c not in taken]
        return free[0] if free else preferred

    async def open(self, key: int, peer: Peer, version: int) -> None:
        old = self.peers.get(key)
        if old is not None and old.id:
            peer.id, peer.color = old.id, old.color
        else:
            self._peer_seq += 1
            peer.id = f"p{self._peer_seq}"
            peer.color = self._assign_color(peer, key)
        peer.sel = None
        self.peers[key] = peer
        self.last_activity = time.monotonic()
        opened = {"t": "opened", "doc": self.doc_id, "version": self.version, "role": peer.role,
                  "clientID": peer.client_id, "peerId": peer.id, "color": peer.color,
                  "peers": [p.public() for k, p in self.peers.items() if k != key]}
        self.broadcast({"t": "presence", "doc": self.doc_id, "version": self.version, "peer": peer.public()},
                       exclude=key)
        if version == self.version:
            peer.send(opened)
            return
        if 0 <= version < self.version:
            rows = await self.db.read(lambda c: svc.ops_since(c, self.doc_id, version))
            if rows and rows[0]["version"] == version and len(rows) == self.version - version:
                peer.send(opened)
                peer.send({"t": "updates", "doc": self.doc_id, "from": version,
                           "updates": [{"clientID": r["client_id"], "changes": json.loads(r["changes"])}
                                       for r in rows]})
                return
        # Unknown or pruned history: the client starts over from the current text.
        peer.send(opened)
        peer.send({"t": "reset", "doc": self.doc_id, "version": self.version, "text": self.text()})

    def leave(self, key: int) -> None:
        peer = self.peers.pop(key, None)
        self.last_activity = time.monotonic()
        if peer is not None and peer.id:
            self.broadcast({"t": "presence", "doc": self.doc_id, "version": self.version,
                            "peer": {"id": peer.id, "gone": True}})

    async def presence(self, key: int, version: int, sel) -> None:
        """Map a peer's selection from *version* to now and share it (§8.7)."""
        peer = self.peers.get(key)
        if peer is None:
            return
        if sel is not None:
            if (not isinstance(sel, list) or not 0 < len(sel) <= MAX_SELECTIONS or not all(
                    isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and not isinstance(x, bool)
                                                                for x in r) for r in sel)):
                return
            changes = self.changes_since(version)
            if changes is None:
                return  # too old or from the future: the next update will do
            base_len = changes[0].length if changes else len(self.buf)
            if any(not 0 <= x <= base_len for r in sel for x in r):
                return
            sel = map_selection(sel, changes)
        peer.sel = sel
        self.broadcast({"t": "presence", "doc": self.doc_id, "version": self.version, "peer": peer.public()},
                       exclude=key)

    async def push(self, peer: Peer, version: int, updates: list) -> bool:
        """Apply a client's updates. Returns False if the push was rejected as stale."""
        permissions.require(peer.role, "edit")
        if not isinstance(updates, list) or not updates or len(updates) > MAX_UPDATES_PER_PUSH:
            raise PushError("invalid_update", "A push carries 1 to 100 updates.")
        parsed: list[ChangeSet] = []
        for u in updates:
            if not isinstance(u, dict) or u.get("clientID") != peer.client_id:
                raise PushError("invalid_update", "Updates must carry this connection's client ID.")
            try:
                parsed.append(ChangeSet.from_json(u.get("changes")))
            except InvalidChange as exc:
                raise PushError("invalid_update", str(exc)) from None
        if version != self.version:
            return False
        if sum(cs.churn() for cs in parsed) > self.rooms.cfg.collab.max_push_churn:
            raise PushError("too_large", "This change is too large. Paste it in smaller parts.")
        new_len = len(self.buf)
        for cs in parsed:
            if cs.length != new_len:
                raise PushError("invalid_update", "The change doesn't match the document length.")
            new_len = cs.new_length
        if new_len > self.rooms.cfg.limits.max_document_bytes:
            raise PushError("too_large", "The document would exceed the size limit.")
        records = [{"clientID": peer.client_id, "changes": cs.to_json()} for cs in parsed]
        await self._commit(parsed, records, author=peer.author)
        return True

    async def _commit(self, changesets: list[ChangeSet], records: list[dict], author: int | str | None,
                      merge_id: str | None = None) -> int:
        """Validate, apply, persist, then broadcast. Reverts everything on failure."""
        start_version = self.version
        undo: list = []
        try:
            for cs in changesets:
                self.buf.validate(cs)
                undo.append(self.buf.apply(cs))
            self.version += len(changesets)
            await self.db.write(lambda c: _commit_ops(c, self.doc_id, start_version, records, author, merge_id))
        except BaseException as exc:
            self.version = start_version
            try:
                for record in reversed(undo):
                    self.buf.revert(record)
            except Exception:  # noqa: BLE001
                log.exception("revert failed; reloading room from the database")
                state = await self.db.read(lambda c: svc.load_state(c, self.doc_id))
                self.buf, self.version = Utf16Buffer(state.text), state.version
            if isinstance(exc, InvalidChange):
                raise PushError("invalid_update", str(exc)) from None
            if isinstance(exc, PushError):
                raise
            log.exception("commit failed", extra=fields(doc=self.doc_id))
            raise PushError("not_saved", "The change could not be saved. It will be sent again.") from None
        self.ops_since_snapshot += len(changesets)
        for i, cs in enumerate(changesets):
            self.recent.append((start_version + i, cs))
        for peer in self.peers.values():
            if peer.sel:
                peer.sel = map_selection(peer.sel, changesets)
        orphaned = self.after_commit(changesets)
        self.broadcast({"t": "updates", "doc": self.doc_id, "from": start_version, "updates": records})
        self.touch()
        if orphaned:
            try:
                await self._orphan(orphaned)
            except Exception:  # noqa: BLE001 - the edit itself is committed
                log.exception("marking comments orphaned failed", extra=fields(doc=self.doc_id))
        if self.ops_since_snapshot >= self.rooms.cfg.collab.snapshot_every:
            await self.snapshot()
        return self.version

    def after_commit(self, changesets: list[ChangeSet]) -> list[tuple[str, int]]:
        """Map everything anchored to the text through committed changes.

        Returns comment threads whose text was deleted entirely (now orphaned)."""
        orphaned = []
        for key, rng in self.anchors.items():
            frm, to = rng
            for cs in changesets:
                frm, to = map_range(cs, frm, to)
            if key[0] == "comment" and rng[1] > rng[0] and frm == to:
                orphaned.append(key)
            rng[0], rng[1] = frm, to
        if self.anchors:
            self.anchors_dirty = True
        return orphaned

    # -- anchors ----------------------------------------------------------
    async def _changes_from(self, version: int) -> list[ChangeSet] | None:
        """Changes from *version* to now, from memory or the operation log."""
        changes = self.changes_since(version)
        if changes is not None or version > self.version:
            return changes
        rows = await self.db.read(lambda c: svc.ops_since(c, self.doc_id, version))
        if not rows or rows[0]["version"] != version or len(rows) < self.version - version:
            return None
        return [ChangeSet.from_json(json.loads(r["changes"])) for r in rows[:self.version - version]]

    async def load_anchors(self) -> None:
        def load(c):
            comments = c.execute(
                "SELECT id, anchor_from, anchor_to, anchor_version FROM comments WHERE doc_id = ? AND id = thread_id"
                " AND resolved_at IS NULL AND orphaned = 0 AND anchor_from IS NOT NULL", (self.doc_id,)).fetchall()
            conflicts = c.execute("SELECT id, anchor_from, anchor_to, anchor_version FROM conflicts"
                                  " WHERE doc_id = ? AND resolved_at IS NULL", (self.doc_id,)).fetchall()
            return [("comment", r) for r in comments] + [("conflict", r) for r in conflicts]

        rows = await self.db.read(load)
        if not rows:
            return
        cache: dict[int, list[ChangeSet] | None] = {}
        orphaned = []
        n = len(self.buf)
        for kind, r in rows:
            frm, to, version = r["anchor_from"], r["anchor_to"], r["anchor_version"]
            if version not in cache:
                cache[version] = await self._changes_from(version)
            for cs in cache[version] or []:
                frm, to = map_range(cs, frm, to)
            frm = max(0, min(frm, n))
            to = max(frm, min(to, n))
            if kind == "comment" and r["anchor_to"] > r["anchor_from"] and frm == to:
                orphaned.append(("comment", r["id"]))
            self.anchors[(kind, r["id"])] = [frm, to]
        self.anchors_dirty = True
        if orphaned:
            await self._orphan(orphaned)

    async def _orphan(self, keys: list[tuple[str, int]]) -> None:
        ids = [k[1] for k in keys]
        for k in keys:
            self.anchors.pop(k, None)
        await self.db.write(lambda c: c.executemany("UPDATE comments SET orphaned = 1 WHERE id = ?",
                                                    [(i,) for i in ids]))
        self.broadcast({"t": "comments", "doc": self.doc_id})

    async def persist_anchors(self) -> None:
        if not self.anchors_dirty:
            return
        version = self.version
        rows = [(rng[0], rng[1], version, key[1]) for key, rng in self.anchors.items()]

        def work(c):
            c.executemany("UPDATE comments SET anchor_from = ?, anchor_to = ?, anchor_version = ? WHERE id = ?",
                          [r for r, key in zip(rows, self.anchors) if key[0] == "comment"])
            c.executemany("UPDATE conflicts SET anchor_from = ?, anchor_to = ?, anchor_version = ? WHERE id = ?",
                          [r for r, key in zip(rows, self.anchors) if key[0] == "conflict"])

        await self.db.write(work)
        self.anchors_dirty = False

    async def anchor_snapshot(self) -> tuple[int, dict]:
        return self.version, {f"{k[0]}:{k[1]}": list(v) for k, v in self.anchors.items()}

    async def add_thread(self, version: int, frm: int, to: int, insert) -> dict:
        """Anchor a new comment thread at [frm, to) of *version*; *insert(c, frm, to, version, quote)*
        writes the row and returns its id."""
        changes = await self._changes_from(version)
        if changes is None:
            raise PushError("stale", "The document changed too much. Select the text again.")
        length = changes[0].length if changes else len(self.buf)
        if not (0 <= frm <= to <= length):
            raise PushError("bad_anchor", "The selection is outside the document.")
        for cs in changes:
            frm, to = map_range(cs, frm, to)
        if not (self.buf.is_boundary(frm) and self.buf.is_boundary(to)):
            raise PushError("bad_anchor", "The selection splits a character.")
        quote = self.buf.slice(frm, to)[:500]
        current = self.version
        thread_id = await self.db.write(lambda c: insert(c, frm, to, current, quote))
        self.anchors[("comment", thread_id)] = [frm, to]
        self.broadcast({"t": "comments", "doc": self.doc_id})
        return {"id": thread_id, "from": frm, "to": to, "version": current}

    async def set_resolved(self, kind: str, item_id: int, resolved: bool, update) -> None:
        """Resolve or reopen a comment thread or conflict; *update(c, anchor)* writes the row."""
        key = (kind, item_id)
        if resolved:
            anchor = self.anchors.pop(key, None)
            await self.db.write(lambda c: update(c, (anchor, self.version) if anchor else None))
        else:
            await self.db.write(lambda c: update(c, None))
            row = await self.db.fetchone(
                f"SELECT anchor_from, anchor_to, anchor_version FROM {'comments' if kind == 'comment' else 'conflicts'}"
                " WHERE id = ?", (item_id,))
            if row is not None and row["anchor_from"] is not None:
                frm, to = row["anchor_from"], row["anchor_to"]
                for cs in await self._changes_from(row["anchor_version"]) or []:
                    frm, to = map_range(cs, frm, to)
                n = len(self.buf)
                self.anchors[key] = [min(frm, n), min(max(frm, to), n)]
                self.anchors_dirty = True
        self.broadcast({"t": "comments" if kind == "comment" else "conflicts", "doc": self.doc_id})

    async def apply_text(self, new_text: str, client_id: str, author: int | str | None = None) -> int:
        """Replace the text with *new_text* as one diff-based update (restores, merges)."""
        cs = from_diff(self.text(), new_text)
        if cs.empty_changes:
            return self.version
        return await self._commit([cs], [{"clientID": client_id, "changes": cs.to_json()}], author)

    async def merge(self, peer: Peer, merge_id: str, base: str, text: str) -> dict:
        """Reconcile a client's offline text (§8.6)."""
        permissions.require(peer.role, "edit")
        prior = await self.db.read(lambda c: c.execute(
            "SELECT version FROM doc_ops WHERE doc_id = ? AND merge_id = ?", (self.doc_id, merge_id)).fetchone())
        if prior is not None:
            return {"t": "merged", "doc": self.doc_id, "mergeId": merge_id, "version": prior[0] + 1, "conflicts": 0}
        conflicts = await self._reconcile(base, text, source=f"offline:{peer.name}", author=peer.author,
                                          label=f"Offline version, {peer.name}", merge_id=merge_id,
                                          client_id=f"merge:{merge_id}")
        return {"t": "merged", "doc": self.doc_id, "mergeId": merge_id, "version": self.version,
                "conflicts": conflicts}

    async def _reconcile(self, base: str, theirs: str, *, source: str, author: int | str | None, label: str,
                         merge_id: str | None = None, client_id: str = "system") -> int:
        ours = self.text()
        result = reconcile(base, ours, theirs)
        if not result.changes.empty_changes:
            await self._commit([result.changes], [{"clientID": client_id, "changes": result.changes.to_json()}],
                               author, merge_id)
        elif merge_id is not None:
            # Record the merge id so a repeated merge gets the same answer.
            cs = ChangeSet.empty(len(self.buf))
            await self._commit([cs], [{"clientID": client_id, "changes": cs.to_json()}], author, merge_id)
        if result.conflicts:
            stamp = time.strftime("%d %b %H:%M", time.gmtime())
            version = self.version

            def record(c):
                c.execute("INSERT INTO doc_snapshots(doc_id, version, text, label, created_by) VALUES (?, ?, ?, ?, ?)",
                          (self.doc_id, version, theirs, f"{label}, {stamp}", user_of(author)))
                ids = []
                for k in result.conflicts:
                    ids.append(c.execute(
                        "INSERT INTO conflicts(doc_id, source, base_text, theirs_text, anchor_from, anchor_to,"
                        " anchor_version, version_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.doc_id, source, k.base_text, k.theirs_text, k.anchor_from, k.anchor_to, version,
                         f"{label}, {stamp}")).lastrowid)
                return ids

            ids = await self.db.write(record)
            for cid, k in zip(ids, result.conflicts):
                self.anchors[("conflict", cid)] = [k.anchor_from, k.anchor_to]
            self.broadcast({"t": "conflicts", "doc": self.doc_id, "count": len(result.conflicts)})
        return len(result.conflicts)

    async def snapshot(self, label: str | None = None, user_id: int | None = None) -> int:
        await self.persist_anchors()
        text, version = self.text(), self.version
        retention = self.rooms.cfg.collab.op_retention_days * 86400

        def work(c):
            c.execute("INSERT INTO doc_snapshots(doc_id, version, text, label, created_by) VALUES (?, ?, ?, ?, ?)",
                      (self.doc_id, version, text, label, user_id))
            if label is None:
                c.execute("UPDATE documents SET snapshot_version = ? WHERE id = ?", (version, self.doc_id))
                # Unlabelled snapshots older than the newest one are no longer needed.
                c.execute("DELETE FROM doc_snapshots WHERE doc_id = ? AND label IS NULL AND version < ?",
                          (self.doc_id, version))
                c.execute("DELETE FROM doc_ops WHERE doc_id = ? AND version < ? AND created_at < ?",
                          (self.doc_id, version, time.time() - retention))

        await self.db.write(work)
        if label is None:
            self.ops_since_snapshot = 0
        return version

    # -- files ------------------------------------------------------------
    async def materialize(self, force: bool = False) -> None:
        """Write the current text to the project folder (§11.2)."""
        await self.persist_anchors()
        text, version = self.text(), self.version
        data = files.encode_text(text, self.line_ending)
        digest = files.sha256(data)
        if (not force and version == self.meta.get("materialized_version")
                and digest == self.meta.get("materialized_sha256")):
            self.dirty_since = None
            return
        await self.db.write(lambda c: c.execute("UPDATE documents SET pending_sha256 = ? WHERE id = ?",
                                                (digest, self.doc_id)))
        data_dir = self.rooms.cfg.data_dir

        def write():
            files.put_blob(data_dir, data)
            return files.atomic_write(self.project_dir, self.path, data)

        st = await asyncio.to_thread(write)
        await self.db.write(lambda c: c.execute(
            "UPDATE documents SET materialized_version = ?, materialized_sha256 = ?, pending_sha256 = NULL,"
            " materialized_stat = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (version, digest, json.dumps(st), self.doc_id)))
        self.meta.update(materialized_version=version, materialized_sha256=digest, pending_sha256=None,
                         materialized_stat=json.dumps(st))
        self.dirty_since = None if self.version == version else self.dirty_since

    async def check_disk(self) -> bool:
        """Detect and reconcile an outside edit of the file. Returns True if one was found."""
        st = await asyncio.to_thread(files.stat_of, self.project_dir, self.path)
        stored = json.loads(self.meta["materialized_stat"]) if self.meta.get("materialized_stat") else None
        if st is None:
            # Missing (never written, or deleted while open): the database is authoritative.
            await self.materialize(force=True)
            return False
        if stored is not None and list(st) == list(stored):
            if self.meta.get("materialized_version", -1) < self.version and self.dirty_since is None:
                await self.materialize()
            return False
        data = await asyncio.to_thread(files.read_bytes, self.project_dir, self.path)
        digest = files.sha256(data)
        if digest == self.meta.get("materialized_sha256"):
            await self.db.write(lambda c: c.execute("UPDATE documents SET materialized_stat = ? WHERE id = ?",
                                                    (json.dumps(st), self.doc_id)))
            self.meta["materialized_stat"] = json.dumps(st)
            return False
        if digest == self.meta.get("pending_sha256"):
            # Our own write from before a crash; rewrite from the database.
            await self.materialize(force=True)
            return False
        try:
            theirs, ending = files.decode_text(data)
        except UnicodeDecodeError:
            files.put_blob(self.rooms.cfg.data_dir, data)
            log.warning("outside edit is not valid UTF-8; keeping the database version",
                        extra=fields(doc=self.doc_id, blob=digest))
            await self.materialize(force=True)
            return False
        base = ""
        if self.meta.get("materialized_sha256"):
            try:
                base_bytes = await asyncio.to_thread(files.get_blob, self.rooms.cfg.data_dir,
                                                     self.meta["materialized_sha256"])
                base, _ = files.decode_text(base_bytes)
            except (OSError, UnicodeDecodeError):
                base = ""
        log.info("reconciling outside edit", extra=fields(doc=self.doc_id, path=self.path))
        await self._reconcile(base, theirs, source="disk", author=None, label="Outside edit",
                              client_id="system:disk")
        await self.materialize(force=True)
        return True


def user_of(author) -> int | None:
    """The user id behind an operation author ("12" or 12); None for guests and the system."""
    if isinstance(author, int):
        return author
    if isinstance(author, str) and author.isdigit():
        return int(author)
    return None


def _commit_ops(conn, doc_id: str, version: int, records: list[dict], author: int | str | None,
                merge_id: str | None) -> None:
    now = time.time()
    for i, rec in enumerate(records):
        conn.execute(
            "INSERT INTO doc_ops(doc_id, version, client_id, author, changes, merge_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, version + i, rec["clientID"], str(author) if author is not None else None,
             json.dumps(rec["changes"], separators=(",", ":")), merge_id if i == 0 else None, now))
    cur = conn.execute("UPDATE documents SET version = ? WHERE id = ? AND version = ?",
                       (version + len(records), doc_id, version))
    if cur.rowcount != 1:
        raise RuntimeError("document version moved underneath the room")


class Rooms:
    """Loads, schedules and unloads rooms; runs the materializer and disk watcher."""

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.rooms: dict[str, Room] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.change_hooks: list[Callable[[Room], None]] = []
        self._last_disk_poll = 0.0

    def project_dir(self, project_id: str) -> Path:
        return self.cfg.data_dir / "projects" / project_id

    async def get(self, doc_id: str) -> Room:
        room = self.rooms.get(doc_id)
        if room is not None and not room.closed:
            return room
        lock = self._locks.setdefault(doc_id, asyncio.Lock())
        async with lock:
            room = self.rooms.get(doc_id)
            if room is not None and not room.closed:
                return room

            def load(c):
                doc = svc.get_document(c, doc_id)
                if doc is None:
                    return None, None
                return dict(doc), svc.load_state(c, doc_id)

            doc, state = await self.app.db.read(load)
            if doc is None:
                raise KeyError(doc_id)
            room = Room(self, doc, state)
            self.rooms[doc_id] = room
            try:
                await room.call(room.load_anchors)
            except Exception:
                log.exception("loading anchors failed", extra=fields(doc=doc_id))
            try:
                # Startup rules (§11.2): compare the file, reconcile, materialize.
                await room.call(room.check_disk)
            except Exception:
                log.exception("checking the file on load failed", extra=fields(doc=doc_id))
            return room

    def loaded(self, doc_id: str) -> Room | None:
        room = self.rooms.get(doc_id)
        return room if room is not None and not room.closed else None

    async def unload(self, room: Room) -> None:
        try:
            await room.call(room.materialize)
            if room.ops_since_snapshot:
                await room.call(room.snapshot)
        finally:
            await room.stop()
            if self.rooms.get(room.doc_id) is room:
                del self.rooms[room.doc_id]

    async def close_document(self, doc_id: str, reason: str = "deleted") -> None:
        room = self.loaded(doc_id)
        if room is None:
            return
        room.broadcast({"t": "error", "code": reason, "doc": doc_id, "message": "This document was removed."})
        room.peers.clear()
        await room.stop()
        self.rooms.pop(doc_id, None)

    # -- background ----------------------------------------------------
    async def maintenance(self) -> None:
        collab = self.cfg.collab
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            poll_disk = now - self._last_disk_poll >= collab.disk_poll_seconds
            if poll_disk:
                self._last_disk_poll = now
            for room in list(self.rooms.values()):
                if room.closed:
                    continue
                try:
                    if room.dirty_since is not None and (now - room.last_op >= collab.materialize_delay
                                                         or now - room.dirty_since >= collab.materialize_max_delay):
                        await room.call(room.materialize)
                    if room.ops_since_snapshot and now - room.last_op >= collab.snapshot_idle_seconds:
                        await room.call(room.snapshot)
                    if poll_disk:
                        await room.call(room.check_disk)
                    if (not room.peers and room.dirty_since is None
                            and now - room.last_activity >= collab.room_idle_seconds):
                        await self.unload(room)
                except Exception:  # noqa: BLE001
                    log.exception("room maintenance failed", extra=fields(doc=room.doc_id))
            if poll_disk:
                for project_id in {r.project_id for r in self.rooms.values()}:
                    try:
                        await self.app.scanner.scan(project_id)
                    except Exception:  # noqa: BLE001
                        log.exception("project scan failed", extra=fields(project=project_id))

    async def flush_all(self) -> None:
        for room in list(self.rooms.values()):
            try:
                await self.unload(room)
            except Exception:  # noqa: BLE001
                log.exception("flushing room failed", extra=fields(doc=room.doc_id))
