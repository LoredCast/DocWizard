"""Backup and restore (§11.4).

The database is authoritative and asset content lives in an immutable blob
store, so a consistent backup needs no global pause: copy the database with
SQLite's online backup API, then the blobs (every blob the copy references
already exists, because blobs are written before rows refer to them), then
the layouts. Restoring rebuilds every project folder from the database.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from ..db.database import Database
from . import files
from . import service as svc

LOCK_NAME = "backup.lock"


def _copy_tree_missing(src: Path, dest: Path) -> int:
    count = 0
    if not src.is_dir():
        return count
    for dirpath, _, filenames in os.walk(src, followlinks=False):
        rel = Path(dirpath).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            s, d = Path(dirpath) / name, dest / rel / name
            if s.is_symlink() or name.endswith(".tmp"):
                continue
            if not d.exists() or d.stat().st_size != s.stat().st_size:
                shutil.copy2(s, d)
                count += 1
    return count


def backup(cfg, dest: str | os.PathLike) -> dict:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    data = cfg.data_dir
    lock = data / LOCK_NAME
    lock.write_text(str(os.getpid()))  # pauses blob garbage collection
    try:
        src = sqlite3.connect(cfg.db_path)
        out = sqlite3.connect(dest / "docwizard.db")
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        blobs = _copy_tree_missing(data / "blobs", dest / "blobs")
        _copy_tree_missing(data / "layouts", dest / "layouts")
    finally:
        lock.unlink(missing_ok=True)
    manifest = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "release": cfg.release,
                "blobs_copied": blobs}
    (dest / "backup.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def restore(cfg, src: str | os.PathLike) -> None:
    """Restore into cfg.data_dir. The app must not be running."""
    src = Path(src)
    if not (src / "docwizard.db").is_file():
        raise SystemExit(f"{src} is not a DocWizard backup (docwizard.db missing).")
    data = cfg.data_dir
    data.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(src / "docwizard.db", cfg.db_path)
    _copy_tree_missing(src / "blobs", data / "blobs")
    _copy_tree_missing(src / "layouts", data / "layouts")
    rebuild_folders(cfg)


def rebuild_folders(cfg) -> None:
    """Write every project folder from the database and blobs."""
    data = cfg.data_dir
    shutil.rmtree(data / "projects", ignore_errors=True)
    db = Database(cfg.db_path, cfg.server.durability)
    db.open()
    try:
        docs = db.read_sync(lambda c: c.execute(
            "SELECT d.id, d.project_id, d.path, d.line_ending FROM documents d JOIN projects p ON p.id = d.project_id"
            " WHERE d.deleted_at IS NULL AND p.deleted_at IS NULL").fetchall())
        for doc in docs:
            state = db.read_sync(lambda c, doc_id=doc["id"]: svc.load_state(c, doc_id))
            payload = files.encode_text(state.text, doc["line_ending"])
            digest = files.put_blob(data, payload)
            st = files.atomic_write(data / "projects" / doc["project_id"], doc["path"], payload)
            db.write_sync(lambda c, doc=doc, state=state, digest=digest, st=st: c.execute(
                "UPDATE documents SET materialized_version = ?, materialized_sha256 = ?, pending_sha256 = NULL,"
                " materialized_stat = ? WHERE id = ?", (state.version, digest, json.dumps(st), doc["id"])))
        assets = db.read_sync(lambda c: c.execute(
            "SELECT a.project_id, a.path, a.sha256 FROM assets a JOIN projects p ON p.id = a.project_id"
            " WHERE a.deleted_at IS NULL AND p.deleted_at IS NULL").fetchall())
        for asset in assets:
            payload = files.get_blob(data, asset["sha256"])
            st = files.atomic_write(data / "projects" / asset["project_id"], asset["path"], payload)
            db.write_sync(lambda c, a=asset, st=st: c.execute(
                "UPDATE assets SET materialized_stat = ? WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                (json.dumps(st), a["project_id"], a["path"])))
    finally:
        db.close()
