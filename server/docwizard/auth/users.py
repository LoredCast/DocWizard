"""Local user accounts."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import passwords

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$")
# Deliberately loose: one @, something on both sides, a dot in the domain, no spaces. The
# confirmation link is the real check.
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


def clean_email(value: str | None) -> str | None:
    """A trimmed email address, None for empty, or ValueError."""
    value = (value or "").strip()
    if not value:
        return None
    if len(value) > 200 or not EMAIL_RE.match(value):
        raise ValueError("This email address doesn't look right.")
    return value


def email_taken(conn: sqlite3.Connection, email: str, exclude_user: int | None = None) -> bool:
    """Whether another account already uses *email* (compared without case)."""
    return conn.execute("SELECT 1 FROM users WHERE email IS NOT NULL AND lower(email) = lower(?) AND id IS NOT ?",
                        (email, exclude_user)).fetchone() is not None


@dataclass
class User:
    id: int
    username: str
    display_name: str
    email: str | None
    instance_role: str
    preferences: dict = field(default_factory=dict)
    totp: bool = False  # two-step login is on
    require_totp: bool = False  # an admin requires it
    email_verified: bool = False  # the email address was confirmed

    @property
    def is_admin(self) -> bool:
        return self.instance_role == "admin"

    @property
    def theme(self) -> str:
        theme = self.preferences.get("theme", "dark")
        return theme if theme in ("dark", "light", "system") else "dark"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "displayName": self.display_name,
                "role": self.instance_role, "preferences": self.preferences}


def _from_row(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    try:
        prefs = json.loads(row["preferences"] or "{}")
    except ValueError:
        prefs = {}
    keys = row.keys()
    return User(row["id"], row["username"], row["display_name"], row["email"], row["instance_role"], prefs,
                totp="totp_secret_enc" in keys and row["totp_secret_enc"] is not None,
                require_totp="require_totp" in keys and bool(row["require_totp"]),
                email_verified="email_verified_at" in keys and row["email_verified_at"] is not None and bool(row["email"]))


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _from_row(conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone())


def get_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create(conn: sqlite3.Connection, username: str, password_hash: str | None, *, display_name: str = "",
           email: str | None = None, role: str = "user", email_verified: bool = False) -> int:
    if not USERNAME_RE.match(username):
        raise ValueError("Usernames use 2–40 letters, digits, dots, dashes or underscores.")
    if get_by_username(conn, username) is not None:
        raise ValueError("This username is taken.")
    return conn.execute(
        "INSERT INTO users(username, email, display_name, password_hash, instance_role, email_verified_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (username, email, display_name or username, password_hash, role,
         time.time() if email and email_verified else None),
    ).lastrowid


def set_email(conn: sqlite3.Connection, user_id: int, email: str | None, verified: bool) -> None:
    conn.execute("UPDATE users SET email = ?, email_verified_at = ? WHERE id = ?",
                 (email, time.time() if email and verified else None, user_id))


def set_password(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def update_profile(conn: sqlite3.Connection, user_id: int, display_name: str, preferences: dict) -> None:
    conn.execute("UPDATE users SET display_name = ?, preferences = ? WHERE id = ?",
                 (display_name, json.dumps(preferences), user_id))


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def authenticate(row: sqlite3.Row | None, password: str) -> bool:
    """Check a password against a user row (or burn the same time if missing)."""
    if row is None or not row["active"]:
        passwords.verify_password(password, None)
        return False
    return passwords.verify_password(password, row["password_hash"])
