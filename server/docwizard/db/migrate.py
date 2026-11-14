"""Forward-only migrations: numbered SQL files applied in order.

``PRAGMA user_version`` records the number of the last applied migration.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def available() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        m = _NAME.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migrations must be numbered 0001, 0002, …; found {numbers}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations. Returns the numbers applied."""
    applied = []
    version = current_version(conn)
    migrations = available()
    if version > len(migrations):
        raise RuntimeError(
            f"database is at migration {version}, newer than this release ({len(migrations)})"
        )
    for number, path in migrations:
        if number <= version:
            continue
        sql = path.read_text()
        # executescript commits implicitly, so wrap each file in its own transaction.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(number)
    return applied
