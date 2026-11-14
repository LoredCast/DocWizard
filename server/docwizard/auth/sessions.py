"""Server-side sessions. The cookie holds a random token; the table its SHA-256 (§13.3)."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

TOUCH_INTERVAL = 300  # seconds between last_seen_at updates


@dataclass
class Session:
    token_hash: bytes
    user_id: int | None
    csrf_token: str
    expires_at: float
    last_seen_at: float


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create(conn: sqlite3.Connection, user_id: int | None, idle_seconds: int, user_agent: str = "") -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    now = time.time()
    sess = Session(token_hash(token), user_id, secrets.token_urlsafe(24), now + idle_seconds, now)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at, user_agent)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sess.token_hash, user_id, sess.csrf_token, now, sess.expires_at, now, user_agent[:200]),
    )
    return token, sess


def load(conn: sqlite3.Connection, token: str) -> Session | None:
    if not token or len(token) > 100:
        return None
    row = conn.execute(
        "SELECT s.token_hash, s.user_id, s.csrf_token, s.expires_at, s.last_seen_at FROM sessions s"
        " LEFT JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND (s.user_id IS NULL OR u.active = 1)",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["expires_at"] < time.time():
        return None
    return Session(row["token_hash"], row["user_id"], row["csrf_token"], row["expires_at"], row["last_seen_at"])


def needs_touch(sess: Session) -> bool:
    return time.time() - sess.last_seen_at > TOUCH_INTERVAL


def touch(conn: sqlite3.Connection, sess: Session, idle_seconds: int) -> None:
    now = time.time()
    conn.execute(
        "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
        (now, now + idle_seconds, sess.token_hash),
    )


def delete(conn: sqlite3.Connection, sess: Session) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sess.token_hash,))


def delete_for_user(conn: sqlite3.Connection, user_id: int, keep: bytes | None = None) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", (user_id, keep))


def cleanup(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),)).rowcount
