"""SQLite access: one writer thread with group commit, per-thread readers.

All writes go through a single writer thread (ARCHITECTURE.md §7.3). Work
queued within a short window is committed together, so with
``synchronous=FULL`` the cost is at most one flush per batch. Each unit of
work runs inside its own savepoint: a failing unit is rolled back on its own
and the others in the batch still commit. Futures resolve only after the
commit returned, so callers never act on uncommitted state.
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

from . import migrate as _migrate

T = TypeVar("T")

GROUP_COMMIT_WINDOW = 0.020  # seconds
_STOP = object()


def connect(path: Path | str, durability: str = "full") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA synchronous={'FULL' if durability == 'full' else 'NORMAL'}")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class Database:
    def __init__(self, path: Path | str, durability: str = "full"):
        self.path = Path(path)
        self.durability = durability
        self._local = threading.local()
        self._queue: queue.Queue = queue.Queue()
        self._writer_conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        # Fault injection for tests: called with the batch size before COMMIT.
        self.before_commit: Callable[[int], None] | None = None

    # -- lifecycle ------------------------------------------------------
    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer_conn = connect(self.path, self.durability)
        _migrate.migrate(self._writer_conn)
        self._thread = threading.Thread(target=self._writer_loop, name="db-writer", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(_STOP)
            self._thread.join()
            self._thread = None
        if self._writer_conn is not None:
            self._writer_conn.close()
            self._writer_conn = None

    # -- reads ----------------------------------------------------------
    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path, self.durability)
            conn.execute("PRAGMA query_only=ON")
            self._local.conn = conn
        return conn

    def read_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._reader()
        conn.execute("BEGIN")
        try:
            return fn(conn)
        finally:
            conn.execute("COMMIT")

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self.read_sync, fn)

    async def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return await self.read(lambda c: c.execute(sql, params).fetchone())

    async def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return await self.read(lambda c: c.execute(sql, params).fetchall())

    # -- writes ---------------------------------------------------------
    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future:
        if self._thread is None:
            raise RuntimeError("database is not open")
        fut: Future = Future()
        self._queue.put((fn, fut))
        return fut

    def write_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.submit(fn).result()

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.wrap_future(self.submit(fn))

    async def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Run one statement; returns lastrowid."""
        return await self.write(lambda c: c.execute(sql, params).lastrowid)

    def _writer_loop(self) -> None:
        conn = self._writer_conn
        assert conn is not None
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            batch = [item]
            deadline = time.monotonic() + GROUP_COMMIT_WINDOW
            stop = False
            while len(batch) < 256:
                remaining = deadline - time.monotonic()
                try:
                    nxt = self._queue.get(timeout=remaining) if remaining > 0 else self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                batch.append(nxt)
            self._run_batch(conn, batch)
            if stop:
                return

    def _run_batch(self, conn: sqlite3.Connection, batch: list) -> None:
        results: list[tuple[Future, bool, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for fn, fut in batch:
                if not fut.set_running_or_notify_cancel():
                    continue
                conn.execute("SAVEPOINT unit")
                try:
                    value = fn(conn)
                except BaseException as exc:  # noqa: BLE001 - reported to the caller
                    conn.execute("ROLLBACK TO unit")
                    conn.execute("RELEASE unit")
                    results.append((fut, False, exc))
                else:
                    conn.execute("RELEASE unit")
                    results.append((fut, True, value))
            if self.before_commit is not None:
                self.before_commit(len(batch))
            conn.execute("COMMIT")
        except BaseException as exc:  # noqa: BLE001
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            for fn, fut in batch:
                if not fut.done():
                    if not fut.running():
                        fut.set_running_or_notify_cancel()
                    fut.set_exception(exc)
            return
        for fut, ok, value in results:
            if ok:
                fut.set_result(value)
            else:
                fut.set_exception(value)
