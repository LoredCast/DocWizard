import asyncio
import sqlite3

import pytest

from docwizard import config
from docwizard.db import migrate


def test_migrations_apply_once(db):
    version = db.read_sync(migrate.current_version)
    assert version == len(migrate.available())
    tables = db.read_sync(
        lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    )
    assert {"users", "sessions", "projects", "documents", "doc_ops", "doc_snapshots"} <= tables


def test_failing_unit_does_not_poison_batch(db):
    def ok(c):
        return c.execute("INSERT INTO settings(key, value_json) VALUES ('a', '1')").lastrowid

    def bad(c):
        c.execute("INSERT INTO settings(key, value_json) VALUES ('b', '1')")
        raise ValueError("boom")

    f1, f2 = db.submit(ok), db.submit(bad)
    f1.result()
    with pytest.raises(ValueError):
        f2.result()
    keys = db.read_sync(lambda c: [r[0] for r in c.execute("SELECT key FROM settings ORDER BY key")])
    assert keys == ["a"]


def test_commit_failure_fails_whole_batch(db):
    def boom(n):
        raise sqlite3.OperationalError("disk I/O error")

    db.before_commit = boom
    fut = db.submit(lambda c: c.execute("INSERT INTO settings VALUES ('x', '1')"))
    with pytest.raises(sqlite3.OperationalError):
        fut.result()
    db.before_commit = None
    assert db.read_sync(lambda c: c.execute("SELECT count(*) FROM settings").fetchone()[0]) == 0


def test_async_write_and_read(db):
    async def main():
        await db.execute("INSERT INTO settings VALUES (?, ?)", ("k", '"v"'))
        row = await db.fetchone("SELECT value_json FROM settings WHERE key = ?", ("k",))
        return row[0]

    assert asyncio.run(main()) == '"v"'


def test_config_env_overrides(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("hunter2\n")
    toml = tmp_path / "docwizard.toml"
    toml.write_text('[server]\npublic_url = "https://write.example.org"\n[render]\nconcurrency = 3\n')
    cfg = config.load(toml, env={"DOCWIZARD_RENDER_CONCURRENCY": "4", "DOCWIZARD_SERVER_SERVE_STATIC": "false"})
    assert cfg.render.concurrency == 4
    assert cfg.server.serve_static is False
    assert cfg.cookie_name == "__Host-docwizard"
    assert cfg.app_origin == "https://write.example.org"
