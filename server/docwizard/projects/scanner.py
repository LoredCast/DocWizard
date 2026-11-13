"""Project folder scanning: picks up files added, changed or removed outside DocWizard (§11.2).

Text documents that are loaded in a room are handled by the room's own disk
check; this scanner imports new files, updates changed assets and moves
documents and assets whose files disappeared to the trash.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ..logs import fields
from . import files
from . import service as svc

log = logging.getLogger("docwizard.scanner")


class Scanner:
    def __init__(self, app):
        self.app = app
        self._locks: dict[str, asyncio.Lock] = {}

    def project_dir(self, project_id: str):
        return self.app.cfg.data_dir / "projects" / project_id

    async def scan(self, project_id: str) -> dict:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            return await self._scan(project_id)

    async def _scan(self, project_id: str) -> dict:
        root = self.project_dir(project_id)
        db = self.app.db
        on_disk = set(await asyncio.to_thread(files.walk, root))

        def known(c):
            docs = {r["path"]: dict(r) for r in c.execute(
                "SELECT id, path, materialized_version, materialized_stat FROM documents"
                " WHERE project_id = ? AND deleted_at IS NULL", (project_id,))}
            assets = {r["path"]: dict(r) for r in c.execute(
                "SELECT path, sha256, materialized_stat FROM assets WHERE project_id = ? AND deleted_at IS NULL",
                (project_id,))}
            return docs, assets

        docs, assets = await db.read(known)
        report = {"imported": [], "trashed": [], "updated": []}
        max_upload = self.app.cfg.limits.max_upload_bytes

        for path in sorted(on_disk - docs.keys() - assets.keys()):
            st = await asyncio.to_thread(files.stat_of, root, path)
            if st is None or st[2] > max_upload:
                continue
            data = await asyncio.to_thread(files.read_bytes, root, path)
            kind = files.kind_for(path)
            if kind is not None:
                try:
                    text, ending = files.decode_text(data)
                except UnicodeDecodeError:
                    log.warning("skipping file that is not valid UTF-8", extra=fields(project=project_id, path=path))
                    continue
                digest = await asyncio.to_thread(files.put_blob, self.app.cfg.data_dir, data)

                def add_doc(c, path=path, text=text, ending=ending, digest=digest, st=st):
                    doc_id = svc.create_document(c, project_id, path, text, ending)
                    c.execute("UPDATE documents SET materialized_version = 0, materialized_sha256 = ?,"
                              " materialized_stat = ? WHERE id = ?", (digest, json.dumps(st), doc_id))

                try:
                    await db.write(add_doc)
                except ValueError:
                    continue
            else:
                digest = await asyncio.to_thread(files.put_blob, self.app.cfg.data_dir, data)
                await db.write(lambda c, path=path, digest=digest, size=len(data), st=st:
                               svc.put_asset(c, project_id, path, digest, size, st))
            report["imported"].append(path)

        rooms = self.app.rooms
        for path, doc in docs.items():
            if path in on_disk or rooms.loaded(doc["id"]) is not None:
                continue
            if doc["materialized_version"] < 0:
                continue  # never written yet; loading the room will write it
            await db.write(lambda c, doc_id=doc["id"]: svc.trash_document(c, doc_id))
            report["trashed"].append(path)

        for path, asset in assets.items():
            if path not in on_disk:
                await db.write(lambda c, path=path: svc.trash_asset(c, project_id, path))
                report["trashed"].append(path)
                continue
            st = await asyncio.to_thread(files.stat_of, root, path)
            stored = json.loads(asset["materialized_stat"]) if asset["materialized_stat"] else None
            if st is None or (stored is not None and list(st) == list(stored)):
                continue
            data = await asyncio.to_thread(files.read_bytes, root, path)
            digest = await asyncio.to_thread(files.put_blob, self.app.cfg.data_dir, data)
            await db.write(lambda c, path=path, digest=digest, size=len(data), st=st:
                           svc.put_asset(c, project_id, path, digest, size, st))
            if digest != asset["sha256"]:
                report["updated"].append(path)
        if any(report.values()):
            log.info("project scan", extra=fields(project=project_id, **{k: len(v) for k, v in report.items()}))
        return report

    async def startup(self) -> None:
        """Startup reconstruction (§11.2): scan every project, then check every document."""
        projects = await self.app.db.fetchall("SELECT id FROM projects WHERE deleted_at IS NULL")
        for row in projects:
            try:
                await self.scan(row["id"])
            except Exception:  # noqa: BLE001
                log.exception("startup scan failed", extra=fields(project=row["id"]))
        docs = await self.app.db.fetchall(
            "SELECT d.id FROM documents d JOIN projects p ON p.id = d.project_id"
            " WHERE d.deleted_at IS NULL AND p.deleted_at IS NULL")
        for row in docs:
            try:
                room = await self.app.rooms.get(row["id"])
                await self.app.rooms.unload(room)
            except Exception:  # noqa: BLE001
                log.exception("startup check failed", extra=fields(doc=row["id"]))
