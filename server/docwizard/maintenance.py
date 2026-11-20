"""Daily housekeeping (§7.6): trash purge, blob garbage collection and stale rows."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import time

from . import activity
from .logs import fields

log = logging.getLogger("docwizard.maintenance")

TRASH_DAYS = 30
BLOB_GRACE_SECONDS = 24 * 3600
CLIENT_DAYS = 30


def _iso(seconds_ago: float) -> str:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Maintenance:
    def __init__(self, app):
        self.app = app

    async def run_forever(self) -> None:
        await asyncio.sleep(600)  # not during startup
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed")
            await asyncio.sleep(24 * 3600)

    async def run_once(self) -> dict:
        report = {**await self.purge_trash(), **await self.prune_rows()}
        report["notifications"] = await self.app.notifier.prune()
        report["blobs"] = await self.collect_blobs()
        log.info("maintenance", extra=fields(**report))
        return report

    async def purge_trash(self) -> dict:
        cutoff = _iso(TRASH_DAYS * 86400)

        def work(c):
            projects = [r[0] for r in c.execute("SELECT id FROM projects WHERE deleted_at IS NOT NULL"
                                                " AND deleted_at < ?", (cutoff,)).fetchall()]
            for pid in projects:
                c.execute("DELETE FROM publications WHERE project_id = ?", (pid,))
                c.execute("DELETE FROM projects WHERE id = ?", (pid,))
            docs = c.execute("DELETE FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                             (cutoff,)).rowcount
            assets = c.execute("DELETE FROM assets WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                               (cutoff,)).rowcount
            return projects, docs, assets

        projects, docs, assets = await self.app.db.write(work)
        self.app.quotas.forget()
        for pid in projects:
            await asyncio.to_thread(shutil.rmtree, self.app.rooms.project_dir(pid), True)
        return {"projects": len(projects), "documents": docs, "assets": assets}

    async def prune_rows(self) -> dict:
        now = time.time()

        def work(c):
            tokens = c.execute("DELETE FROM account_tokens WHERE expires_at < ?", (now - 86400,)).rowcount
            clients = c.execute("DELETE FROM collab_clients WHERE last_seen_at < ?",
                                (now - CLIENT_DAYS * 86400,)).rowcount
            activity.prune(c, now)
            return tokens, clients

        tokens, clients = await self.app.db.write(work)
        return {"tokens": tokens, "clients": clients}

    async def collect_blobs(self) -> int:
        """Delete blobs no row refers to, once they are older than a day. Paused during a backup."""
        from .projects.backup import LOCK_NAME

        data = self.app.cfg.data_dir
        if (data / LOCK_NAME).exists():
            return 0

        def referenced(c):
            refs = set()
            for sql in ("SELECT sha256 FROM assets", "SELECT materialized_sha256 FROM documents",
                        "SELECT pending_sha256 FROM documents", "SELECT base_sha256 FROM sync_state"):
                refs.update(r[0] for r in c.execute(sql) if r[0])
            return refs

        refs = await self.app.db.read(referenced)
        cutoff = time.time() - BLOB_GRACE_SECONDS

        def sweep() -> int:
            removed = 0
            blobs = data / "blobs"
            if not blobs.is_dir():
                return 0
            for entry in os.scandir(blobs):
                if entry.name in refs or not entry.is_file() or len(entry.name) != 64:
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
            return removed

        return await asyncio.to_thread(sweep)
