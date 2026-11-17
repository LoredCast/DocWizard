"""Share links and guests (§14.1, §14.2).

A link is a random 128-bit token, stored hashed, with a role, an optional
expiry and an optional password. Guests are created from a link with a
display name; they only ever see that link's project, and lose access when
the link is revoked or expires.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from ..auth import permissions

LINK_ROLES = ("viewer", "commenter", "editor")


@dataclass
class Guest:
    id: int
    display_name: str
    project_id: str
    share_link_id: int
    role: str
    review: bool = False

    @property
    def label(self) -> str:
        return f"{self.display_name} (guest)"

    def public(self) -> dict:
        return {"id": f"g{self.id}", "displayName": self.label, "guest": True, "projectId": self.project_id,
                "role": self.role, "preferences": {}}


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token() -> str:
    return secrets.token_urlsafe(16)  # 128 bits


def cap_role(role: str, max_role: str) -> str:
    """The lower of *role* and the instance's cap."""
    return role if permissions.rank(role) <= permissions.rank(max_role) else max_role


def create(conn: sqlite3.Connection, project_id: str, role: str, created_by: int | None, *,
           expires_at: float | None = None, password_hash: str | None = None, label: str | None = None,
           review: bool = False) -> tuple[int, str]:
    if role not in LINK_ROLES:
        raise ValueError("Choose editor, commenter or viewer.")
    token = new_token()
    link_id = conn.execute(
        "INSERT INTO share_links(project_id, token_hash, role, label, expires_at, password_hash, created_by, review)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, token_hash(token), role, (label or "").strip()[:80] or None, expires_at, password_hash,
         created_by, int(review))).lastrowid
    return link_id, token


def find(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """A usable link: not revoked, not expired, for a project that exists."""
    if not token or len(token) > 64:
        return None
    row = conn.execute(
        "SELECT l.*, p.name AS project_name FROM share_links l JOIN projects p ON p.id = l.project_id"
        " WHERE l.token_hash = ? AND l.revoked_at IS NULL AND p.deleted_at IS NULL",
        (token_hash(token),)).fetchone()
    if row is None or (row["expires_at"] is not None and row["expires_at"] < time.time()):
        return None
    return row


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT l.id, l.role, l.label, l.expires_at, l.password_hash IS NOT NULL AS has_password, l.created_at, l.review,"
        " u.display_name AS created_by,"
        " (SELECT count(*) FROM guests g WHERE g.share_link_id = l.id) AS guests"
        " FROM share_links l LEFT JOIN users u ON u.id = l.created_by"
        " WHERE l.project_id = ? AND l.revoked_at IS NULL ORDER BY l.id DESC", (project_id,)).fetchall()
    now = time.time()
    out = []
    for r in rows:
        item = dict(r)
        item["has_password"] = bool(item["has_password"])
        item["review"] = bool(item["review"])
        item["expired"] = item["expires_at"] is not None and item["expires_at"] < now
        out.append(item)
    return out


def revoke(conn: sqlite3.Connection, project_id: str, link_id: int) -> bool:
    cur = conn.execute("UPDATE share_links SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                       " WHERE id = ? AND project_id = ? AND revoked_at IS NULL", (link_id, project_id))
    # Guests of a revoked link are signed out at once.
    conn.execute("DELETE FROM sessions WHERE guest_id IN (SELECT id FROM guests WHERE share_link_id = ?)",
                 (link_id,))
    return cur.rowcount == 1


def create_guest(conn: sqlite3.Connection, link: sqlite3.Row, display_name: str) -> int:
    name = " ".join(display_name.split())[:60]
    if not name:
        raise ValueError("Enter a name, so others know who you are.")
    return conn.execute("INSERT INTO guests(display_name, share_link_id, project_id) VALUES (?, ?, ?)",
                        (name, link["id"], link["project_id"])).lastrowid


def load_guest(conn: sqlite3.Connection, guest_id: int, max_role: str) -> Guest | None:
    """The guest with their current role, or None once their link is gone."""
    row = conn.execute(
        "SELECT g.id, g.display_name, g.project_id, g.share_link_id, l.role, l.expires_at, l.review"
        " FROM guests g JOIN share_links l ON l.id = g.share_link_id"
        " JOIN projects p ON p.id = g.project_id"
        " WHERE g.id = ? AND l.revoked_at IS NULL AND p.deleted_at IS NULL", (guest_id,)).fetchone()
    if row is None or (row["expires_at"] is not None and row["expires_at"] < time.time()):
        return None
    return Guest(row["id"], row["display_name"], row["project_id"], row["share_link_id"],
                 cap_role(row["role"], max_role), bool(row["review"]))


def cleanup(conn: sqlite3.Connection) -> int:
    """Guests without a session are gone for good; their comments keep their name."""
    return conn.execute("DELETE FROM guests WHERE id NOT IN (SELECT guest_id FROM sessions WHERE guest_id IS NOT NULL)"
                        " AND created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')").rowcount
