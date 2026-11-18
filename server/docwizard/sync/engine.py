"""Two-way WebDAV sync of a project folder (§12.1, §12.2).

For every file the engine keeps the base: its content (a blob) and remote ETag at
the last successful transfer. Each poll compares the local and the remote side
with the base and acts on the table in §12.2. Text documents that changed on
both sides are reconciled like any outside edit (§12.3): non-overlapping changes
are applied, overlapping ones become conflicts, and the remote text is kept as a
version and as a conflict copy next to the file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import time
from dataclasses import dataclass
from urllib.parse import quote

from ..logs import fields
from ..netpolicy import FetchError, PolicyError
from ..projects import files
from ..projects import service as svc
from ..projects.quota import QuotaExceeded
from ..secretbox import SecretError, context_for
from .folder import FolderRemote
from .git import GitError, GitRemote
from .webdav import DavError, Entry, WebDav

log = logging.getLogger("docwizard.sync")

KIND_LABELS = {"nextcloud": "Nextcloud", "seafile": "Seafile", "webdav": "WebDAV", "folder": "Folder", "git": "Git"}
MAX_BACKOFF = 3600
SETTLE_SECONDS = 5.0


class SyncError(Exception):
    pass


@dataclass
class Local:
    kind: str            # "doc" or "asset"
    path: str
    sha: str
    doc_id: str | None = None
    line_ending: str = "lf"


def conflict_name(path: str, when: float | None = None) -> str:
    stem, ext = posixpath.splitext(path)
    stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(when or time.time()))
    return f"{stem} (conflict {stamp}){ext}"


def _ignored(path: str) -> bool:
    if any(part.startswith(".") for part in path.rstrip("/").split("/")):
        return True
    try:
        return files.normalize(path.rstrip("/")) != path.rstrip("/")
    except files.PathError:
        return True


class SyncEngine:
    def __init__(self, app):
        self.app = app
        self._locks: dict[int, asyncio.Lock] = {}
        self._nudged: dict[str, float] = {}
        app.rooms.change_hooks.append(lambda room: self.nudge(room.project_id))

    # -- scheduling -------------------------------------------------------
    def nudge(self, project_id: str) -> None:
        """Local changes: sync a few seconds after they settle (§12.5)."""
        self._nudged[project_id] = time.monotonic()

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("sync scheduling failed")

    async def tick(self) -> None:
        now = time.time()
        settled = [pid for pid, t in self._nudged.items() if time.monotonic() - t >= SETTLE_SECONDS]
        rows = await self.app.db.fetchall(
            "SELECT t.id, t.project_id, t.next_poll_at, t.status FROM sync_targets t"
            " JOIN projects p ON p.id = t.project_id AND p.deleted_at IS NULL WHERE t.status != 'paused'")
        for r in rows:
            nudged = r["project_id"] in settled and r["status"] != "error"
            if nudged or r["next_poll_at"] <= now:
                self._nudged.pop(r["project_id"], None)
                lock = self._locks.setdefault(r["id"], asyncio.Lock())
                if not lock.locked():
                    self.app.spawn(self.sync_target(r["id"]), f"sync-{r['id']}")
        for pid in settled:
            self._nudged.pop(pid, None)

    # -- one run ----------------------------------------------------------
    async def sync_target(self, target_id: int) -> dict:
        lock = self._locks.setdefault(target_id, asyncio.Lock())
        async with lock:
            target = await self.app.db.fetchone("SELECT * FROM sync_targets WHERE id = ?", (target_id,))
            if target is None:
                return {}
            target = dict(target)
            started = time.time()
            try:
                report = await _Run(self.app, target).run()
            except (DavError, FetchError, PolicyError, SecretError, SyncError, GitError, OSError) as exc:
                failures = target["failures"] + 1
                delay = min(MAX_BACKOFF, target["poll_seconds"] * 2 ** min(failures, 12))
                await self.app.db.execute(
                    "UPDATE sync_targets SET status = 'error', last_error = ?, failures = ?, next_poll_at = ?"
                    " WHERE id = ?", (str(exc)[:500], failures, time.time() + delay, target_id))
                log.warning("sync failed", extra=fields(target=target_id, error=str(exc), retry_in=delay))
                return {"error": str(exc)}
            await self.app.db.execute(
                "UPDATE sync_targets SET status = 'ok', last_error = NULL, failures = 0, last_sync_at = ?,"
                " next_poll_at = ? WHERE id = ?", (started, time.time() + target["poll_seconds"], target_id))
            if any(report.values()):
                log.info("sync", extra=fields(target=target_id, **{k: len(v) for k, v in report.items()}))
            return report


def git_workdir(app, target_id: int):
    return app.cfg.data_dir / "sync-git" / str(target_id)


def git_author(target: dict) -> tuple[str, str]:
    return (target.get("author_name") or "DocWizard", target.get("author_email") or "docwizard@localhost")


def client_for(app, target: dict):
    """The remote side of a target: a WebDAV server, a folder on this server (§12.6) or a git clone (§12.7)."""
    if target["kind"] == "folder":
        from pathlib import Path

        from .folder import allowed_root
        try:
            base, prefix = allowed_root(app.cfg.sync.folder_roots, str(Path(target["base_url"]) / target["remote_path"]))
        except ValueError as exc:
            raise SyncError(str(exc)) from None
        return FolderRemote(base, prefix)
    password = ""
    if target.get("secret_enc"):
        password = app.secrets.open(target["secret_enc"], context_for("sync_targets", target["id"]))
    if target["kind"] == "git":
        if not app.cfg.sync.git:
            raise SyncError("Git sync is turned off on this server.")
        return GitRemote(app.net, git_workdir(app, target["id"]), target["base_url"], target["branch"],
                         username=target["username"], token=password, author=git_author(target))
    root = target["base_url"].rstrip("/")
    if target["remote_path"]:
        root += "/" + quote(target["remote_path"].strip("/"), safe="/")
    return WebDav(app.net, root, target["username"], password,
                  max_bytes=app.cfg.limits.max_upload_bytes)


class _Run:
    def __init__(self, app, target: dict):
        self.app = app
        self.db = app.db
        self.target = target
        self.tid = target["id"]
        self.pid = target["project_id"]
        self.label = KIND_LABELS.get(target["kind"], "WebDAV")
        self.dav = client_for(app, target)
        self.root = app.rooms.project_dir(self.pid)
        self.report: dict[str, list[str]] = {"uploaded": [], "downloaded": [], "deleted_remote": [],
                                             "deleted_local": [], "renamed": [], "conflicts": []}
        self.downloaded: dict[str, tuple[bytes, str | None]] = {}

    async def dav_call(self, fn, *args, **kw):
        return await asyncio.to_thread(fn, *args, **kw)

    # -- state ------------------------------------------------------------
    async def load_base(self) -> dict[str, dict]:
        rows = await self.db.fetchall("SELECT * FROM sync_state WHERE target_id = ?", (self.tid,))
        return {r["path"]: dict(r) for r in rows}

    async def set_base(self, path: str, data: bytes | None, etag: str | None, fileid: str | None,
                       item_id: str | None, sha: str | None = None) -> None:
        if data is not None:
            sha = await asyncio.to_thread(files.put_blob, self.app.cfg.data_dir, data)
        await self.db.execute(
            "INSERT INTO sync_state(target_id, path, item_id, base_sha256, remote_etag, remote_fileid, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(target_id, path) DO UPDATE SET item_id = excluded.item_id,"
            " base_sha256 = excluded.base_sha256, remote_etag = excluded.remote_etag,"
            " remote_fileid = excluded.remote_fileid, updated_at = excluded.updated_at",
            (self.tid, path, item_id, sha, etag, fileid, time.time()))

    async def drop_base(self, path: str) -> None:
        await self.db.execute("DELETE FROM sync_state WHERE target_id = ? AND path = ?", (self.tid, path))

    async def base_bytes(self, row: dict) -> bytes:
        try:
            return await asyncio.to_thread(files.get_blob, self.app.cfg.data_dir, row["base_sha256"])
        except OSError:
            return b""

    # -- local side -------------------------------------------------------
    async def local_items(self) -> dict[str, Local]:
        def load(c):
            docs = c.execute("SELECT id, path, version, materialized_version, materialized_sha256, line_ending"
                             " FROM documents WHERE project_id = ? AND deleted_at IS NULL", (self.pid,)).fetchall()
            assets = c.execute("SELECT path, sha256 FROM assets WHERE project_id = ? AND deleted_at IS NULL",
                               (self.pid,)).fetchall()
            return [dict(d) for d in docs], [dict(a) for a in assets]

        docs, assets = await self.db.read(load)
        out: dict[str, Local] = {}
        for d in docs:
            room = self.app.rooms.loaded(d["id"])
            if room is None and d["materialized_version"] == d["version"] and d["materialized_sha256"]:
                sha = d["materialized_sha256"]
            else:
                sha = files.sha256(await self.doc_bytes(d["id"], d["line_ending"]))
            out[d["path"]] = Local("doc", d["path"], sha, d["id"], d["line_ending"])
        for a in assets:
            out[a["path"]] = Local("asset", a["path"], a["sha256"])
        return out

    async def doc_bytes(self, doc_id: str, ending: str) -> bytes:
        room = self.app.rooms.loaded(doc_id)
        if room is not None:
            text = await room.call(lambda: _text(room))
        else:
            state = await self.db.read(lambda c: svc.load_state(c, doc_id))
            text = state.text
        return files.encode_text(text, ending)

    async def local_bytes(self, item: Local) -> bytes:
        if item.kind == "doc":
            return await self.doc_bytes(item.doc_id, item.line_ending)
        return await asyncio.to_thread(files.get_blob, self.app.cfg.data_dir, item.sha)

    async def create_local(self, path: str, data: bytes) -> Local | None:
        """A file that arrived from the remote side becomes a document or an asset."""
        await self.check_quota(len(data), new_file=True)
        if files.kind_for(path) is not None:
            try:
                text, ending = files.decode_text(data)
            except UnicodeDecodeError:
                log.warning("skipping remote text file that is not UTF-8", extra=fields(target=self.tid, path=path))
                return None
            try:
                doc_id = await self.db.write(lambda c: svc.create_document(c, self.pid, path, text, ending))
            except ValueError:
                return None
            room = await self.app.rooms.get(doc_id)
            await room.call(room.materialize)
            return Local("doc", path, files.sha256(data), doc_id, ending)
        sha = await self.write_asset(path, data)
        return Local("asset", path, sha)

    async def check_quota(self, growth: int, new_file: bool = False) -> None:
        """Files from the remote side count against the project owner's storage (§7.7). Going over
        stops the run with an error the project page shows; files already pulled stay."""
        try:
            await self.app.quotas.check(self.pid, growth, new_files=int(new_file))
        except QuotaExceeded as exc:
            raise SyncError(exc.message) from None

    async def write_asset(self, path: str, data: bytes) -> str:
        def store():
            digest = files.put_blob(self.app.cfg.data_dir, data)
            st = files.atomic_write(self.root, path, data)
            return digest, st

        digest, st = await asyncio.to_thread(store)
        await self.db.write(lambda c: svc.put_asset(c, self.pid, path, digest, len(data), st))
        return digest

    async def remove_local(self, item: Local) -> None:
        if item.kind == "doc":
            await self.db.write(lambda c: svc.trash_document(c, item.doc_id))
            await self.app.rooms.close_document(item.doc_id)
        else:
            await self.db.write(lambda c: svc.trash_asset(c, self.pid, item.path))
        await asyncio.to_thread(files.remove, self.root, item.path)

    async def rename_local(self, item: Local, new_path: str) -> None:
        if item.kind == "doc":
            doc = await self.db.read(lambda c: svc.get_document(c, item.doc_id))
            await self.db.write(lambda c: svc.rename_document(c, doc, new_path))
            room = await self.app.rooms.get(item.doc_id)

            async def move():
                room.path = new_path
                await room.materialize(force=True)
                await asyncio.to_thread(files.remove, self.root, item.path)

            await room.call(move)
        else:
            data = await asyncio.to_thread(files.get_blob, self.app.cfg.data_dir, item.sha)
            st = await asyncio.to_thread(files.atomic_write, self.root, new_path, data)
            await self.db.write(lambda c: c.execute(
                "UPDATE assets SET path = ?, materialized_stat = ? WHERE project_id = ? AND path = ?"
                " AND deleted_at IS NULL", (new_path, json.dumps(st), self.pid, item.path)))
            await asyncio.to_thread(files.remove, self.root, item.path)
        item.path = new_path

    async def apply_remote_text(self, item: Local, base: bytes, theirs: bytes) -> int:
        """Reconcile a remote text into the document; returns the number of conflicts."""
        try:
            base_text, _ = files.decode_text(base) if base else ("", "lf")
            their_text, _ = files.decode_text(theirs)
        except UnicodeDecodeError:
            raise SyncError(f"{item.path} on the server is not valid UTF-8.") from None
        await self.check_quota(len(theirs) - len(base))
        room = await self.app.rooms.get(item.doc_id)
        conflicts = await room.call(lambda: room.reconcile_outside(
            base_text, their_text, source=f"sync:{self.target['kind']}", label=f"Remote version, {self.label}",
            author="sync"))
        await room.call(lambda: room.materialize(force=True))
        return conflicts

    # -- remote side --------------------------------------------------------
    async def download(self, path: str) -> tuple[bytes, str | None]:
        if path not in self.downloaded:
            self.downloaded[path] = await self.dav_call(self.dav.get, path)
        return self.downloaded[path]

    async def upload(self, path: str, data: bytes, *, etag: str | None, new: bool) -> str | None:
        if new:
            await self.dav_call(self.dav.makedirs, path)
        return await self.dav_call(self.dav.put, path, data, if_match=None if new else etag, if_none_match=new)

    # -- the run --------------------------------------------------------------
    async def run(self) -> dict:
        if isinstance(self.dav, GitRemote):
            return await self.run_git()
        await self.ensure_root()
        return await self.run_files()

    async def run_git(self) -> dict:
        """Fetch, sync with the clone like a folder, then commit and push (§12.7).

        If the push fails, the bases go back to what they were, so the next run compares
        both sides with the last state that really reached the repository.
        """
        await self.dav_call(self.dav.begin)
        if self.dav.fresh:
            await self.db.execute("DELETE FROM sync_state WHERE target_id = ?", (self.tid,))
        saved = await self.db.fetchall("SELECT * FROM sync_state WHERE target_id = ?", (self.tid,))
        report = await self.run_files()
        try:
            await self.dav_call(self.dav.finish, commit_message(report))
        except Exception:
            def restore(c):
                c.execute("DELETE FROM sync_state WHERE target_id = ?", (self.tid,))
                c.executemany(
                    "INSERT INTO sync_state(target_id, path, item_id, base_sha256, remote_etag, remote_fileid,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(r["target_id"], r["path"], r["item_id"], r["base_sha256"], r["remote_etag"],
                      r["remote_fileid"], r["updated_at"]) for r in saved])
            await self.db.write(restore)
            raise
        return report

    async def run_files(self) -> dict:
        base = await self.load_base()
        dir_etags = {p: r["remote_etag"] for p, r in base.items() if p.endswith("/") and r["remote_etag"]}
        remote_files, folders, skipped = await self.dav_call(self.dav.walk, dir_etags)
        remote: dict[str, Entry] = {p: e for p, e in remote_files.items() if not _ignored(p)}
        # Files in unchanged folders weren't listed: they are as the base says.
        for p, r in base.items():
            if not p.endswith("/") and any(p.startswith(s) for s in skipped):
                remote[p] = Entry(p, False, r["remote_etag"], 0, r["remote_fileid"])
        local = await self.local_items()
        limit = self.app.cfg.sync.max_files_per_target
        if len(set(local) | set(remote)) > limit:
            raise SyncError(f"More than {limit} files: sync is limited to {limit} files per project.")

        await self.outgoing_renames(base, local, remote)
        await self.incoming_renames(base, local, remote)

        for path in sorted(set(local) | set(remote) | {p for p in base if not p.endswith("/")}):
            await self.reconcile_path(path, base.get(path), local.get(path), remote.get(path))

        # Folder ETags, for skipping unchanged subtrees next time.
        for p, e in folders.items():
            if p and e.etag:
                await self.set_base(p, None, e.etag, e.fileid, None, sha=None)
        for p in base:
            if p.endswith("/") and p not in folders and not any(p.startswith(s) for s in skipped):
                await self.drop_base(p)
        return self.report

    async def ensure_root(self) -> None:
        if isinstance(self.dav, FolderRemote):
            await self.dav_call(self.dav.ensure_root)
            return
        remote_path = self.target["remote_path"].strip("/")
        if not remote_path:
            return
        parent = WebDav(self.dav.policy, self.target["base_url"])
        parent.headers = self.dav.headers
        parent.max_bytes = self.dav.max_bytes
        await self.dav_call(parent.makedirs, remote_path + "/x")

    async def outgoing_renames(self, base, local, remote) -> None:
        """Documents renamed in DocWizard are sent as MOVE, so they keep their identity remotely."""
        by_id = {item.doc_id: item for item in local.values() if item.doc_id}
        for old_path, row in list(base.items()):
            item = by_id.get(row["item_id"]) if row["item_id"] else None
            if item is None or item.path == old_path or old_path in local or item.path in base:
                continue
            entry = remote.get(old_path)
            if entry is None or entry.etag != row["remote_etag"] or item.path in remote:
                continue
            await self.dav_call(self.dav.makedirs, item.path)
            await self.dav_call(self.dav.move, old_path, item.path)
            await self.db.execute("UPDATE sync_state SET path = ? WHERE target_id = ? AND path = ?",
                                  (item.path, self.tid, old_path))
            base[item.path] = {**base.pop(old_path), "path": item.path}
            remote[item.path] = Entry(item.path, False, entry.etag, entry.size, entry.fileid)
            del remote[old_path]
            self.report["renamed"].append(f"{old_path} -> {item.path}")

    async def incoming_renames(self, base, local, remote) -> None:
        """A remote delete plus a remote create in one poll is a rename (§12.2)."""
        gone = {p: r for p, r in base.items() if not p.endswith("/") and p not in remote and p in local
                and local[p].sha == r["base_sha256"]}
        if not gone:
            return
        created = [p for p in remote if p not in base and p not in local]
        for new_path in created:
            entry = remote[new_path]
            match = None
            if entry.fileid:
                match = next((p for p, r in gone.items() if r["remote_fileid"] == entry.fileid), None)
            if match is None and not entry.fileid:
                data, _ = await self.download(new_path)
                sha = files.sha256(data)
                match = next((p for p, r in gone.items() if r["base_sha256"] == sha), None)
            if match is None or files.kind_for(match) != files.kind_for(new_path):
                continue
            item = local.pop(match)
            await self.rename_local(item, new_path)
            local[new_path] = item
            row = base.pop(match)
            del gone[match]
            await self.db.execute("UPDATE sync_state SET path = ? WHERE target_id = ? AND path = ?",
                                  (new_path, self.tid, match))
            base[new_path] = {**row, "path": new_path}
            self.report["renamed"].append(f"{match} -> {new_path}")

    async def reconcile_path(self, path: str, row: dict | None, item: Local | None, entry: Entry | None) -> None:
        if _ignored(path):
            return
        if entry is not None and entry.size > self.app.cfg.limits.max_upload_bytes:
            return
        item_id = item.doc_id if item else (row["item_id"] if row else None)
        if row is None:
            if item is not None and entry is None:
                data = await self.local_bytes(item)
                try:
                    etag = await self.upload(path, data, etag=None, new=True)
                except DavError as exc:
                    if exc.status == 412:
                        return  # created remotely meanwhile: next poll
                    raise
                await self.set_base(path, data, etag, None, item_id)
                self.report["uploaded"].append(path)
            elif item is None and entry is not None:
                data, etag = await self.download(path)
                created = await self.create_local(path, data)
                if created is not None:
                    await self.set_base(path, data, etag or entry.etag, entry.fileid, created.doc_id)
                    self.report["downloaded"].append(path)
            elif item is not None and entry is not None:
                # New on both sides.
                data, etag = await self.download(path)
                if files.sha256(data) == item.sha:
                    await self.set_base(path, data, etag or entry.etag, entry.fileid, item_id)
                else:
                    await self.keep_both(path, item, data, etag or entry.etag, entry.fileid)
            return

        base_sha = row["base_sha256"]
        local_changed = item is not None and item.sha != base_sha
        if item is not None and entry is not None:
            remote_changed = entry.etag != row["remote_etag"]
            if not local_changed and not remote_changed:
                if entry.fileid and entry.fileid != row["remote_fileid"]:
                    # Uploads learn their oc:fileid from the next listing; renames pair by it.
                    await self.db.execute("UPDATE sync_state SET remote_fileid = ? WHERE target_id = ? AND path = ?",
                                          (entry.fileid, self.tid, path))
                return
            if local_changed and not remote_changed:
                await self.push(path, item, row, entry)
                return
            data, etag = await self.download(path)
            etag = etag or entry.etag
            sha = files.sha256(data)
            if sha == base_sha:
                # Only the ETag moved (or our own earlier upload): record it, then push if needed.
                await self.set_base(path, None, etag, entry.fileid, item_id, sha=base_sha)
                if local_changed:
                    await self.push(path, item, {**row, "remote_etag": etag}, entry)
                return
            if sha == item.sha:
                await self.set_base(path, data, etag, entry.fileid, item_id)
                return
            if not local_changed:
                if item.kind == "doc":
                    await self.apply_remote_text(item, await self.base_bytes(row), data)
                else:
                    await self.check_quota(len(data))
                    await self.write_asset(path, data)
                await self.set_base(path, data, etag, entry.fileid, item_id)
                self.report["downloaded"].append(path)
                return
            # Changed on both sides.
            if item.kind == "doc":
                conflicts = await self.apply_remote_text(item, await self.base_bytes(row), data)
                merged = await self.local_bytes(item)
                if conflicts:
                    await self.write_conflict_copy(path, data)
                    self.report["conflicts"].append(path)
                if files.sha256(merged) != sha:
                    try:
                        new_etag = await self.upload(path, merged, etag=etag, new=False)
                    except DavError as exc:
                        if exc.status != 412:
                            raise
                        await self.set_base(path, data, etag, entry.fileid, item_id)
                        return
                    await self.set_base(path, merged, new_etag, entry.fileid, item_id)
                else:
                    await self.set_base(path, data, etag, entry.fileid, item_id)
            else:
                await self.keep_both(path, item, data, etag, entry.fileid)
            return

        if item is not None and entry is None:
            if local_changed:
                data = await self.local_bytes(item)
                etag = await self.upload(path, data, etag=None, new=True)
                await self.set_base(path, data, etag, None, item_id)
                self.report["uploaded"].append(path)
            else:
                await self.remove_local(item)
                await self.drop_base(path)
                self.report["deleted_local"].append(path)
            return

        if item is None and entry is not None:
            if entry.etag == row["remote_etag"]:
                await self.dav_call(self.dav.delete, path, if_match=entry.etag)
                await self.drop_base(path)
                self.report["deleted_remote"].append(path)
            else:
                data, etag = await self.download(path)
                created = await self.create_local(path, data)
                if created is not None:
                    await self.set_base(path, data, etag or entry.etag, entry.fileid, created.doc_id)
                    self.report["downloaded"].append(path)
            return

        await self.drop_base(path)  # gone on both sides

    async def push(self, path: str, item: Local, row: dict, entry: Entry) -> None:
        data = await self.local_bytes(item)
        try:
            etag = await self.upload(path, data, etag=row["remote_etag"], new=False)
        except DavError as exc:
            if exc.status == 412:
                return  # changed remotely meanwhile: the next poll reconciles
            raise
        await self.set_base(path, data, etag, entry.fileid, item.doc_id)
        self.report["uploaded"].append(path)

    async def keep_both(self, path: str, item: Local, theirs: bytes, etag: str | None, fileid: str | None) -> None:
        """Binary (or new-on-both-sides) conflicts: keep ours, save theirs as a conflict copy."""
        await self.write_conflict_copy(path, theirs)
        data = await self.local_bytes(item)
        try:
            new_etag = await self.upload(path, data, etag=etag, new=False)
        except DavError as exc:
            if exc.status != 412:
                raise
            return
        await self.set_base(path, data, new_etag, fileid, item.doc_id)
        self.report["conflicts"].append(path)

    async def write_conflict_copy(self, path: str, data: bytes) -> None:
        name = conflict_name(path)
        exists = await self.db.read(lambda c: svc.path_taken(c, self.pid, name))
        if not exists:
            await self.create_local(name, data)
            self.app.sync.nudge(self.pid)  # uploaded as a new file on the next run


async def _text(room) -> str:
    return room.text()


def commit_message(report: dict) -> str:
    """A commit message that says what DocWizard changed, e.g. "Update paper.md and 2 other files"."""
    changed = report["uploaded"] + report["deleted_remote"] + [r.split(" -> ")[-1] for r in report["renamed"]]
    if not changed:
        return "Update from DocWizard"
    verb = "Delete" if not report["uploaded"] and not report["renamed"] and report["deleted_remote"] else "Update"
    first = changed[0]
    rest = len(set(changed)) - 1
    head = f"{verb} {first}" + ("" if rest == 0 else f" and {rest} other file" + ("s" if rest > 1 else ""))
    lines = [f"- {p}" for p in report["uploaded"]] + [f"- {p} (deleted)" for p in report["deleted_remote"]] \
        + [f"- {r} (renamed)" for r in report["renamed"]]
    return head + "\n\nFrom DocWizard:\n" + "\n".join(lines) + "\n"
