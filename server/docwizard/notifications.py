"""Notifications about comments (§8.9): mentions, replies and suggestions.

Writing ``@username`` in a comment notifies that project member. Replying to a thread
notifies everyone with an account who wrote in it. Notifications show on the home page,
on the notifications page and in the editor; unread ones are mailed after a short delay
(so people who are in the document anyway get no mail), several per message, if mail is
set up and the person hasn't turned it off in their settings.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import sqlite3
from collections import defaultdict

from .logs import fields
from .mail import MailError

log = logging.getLogger("docwizard.notifications")

MENTION = re.compile(r"(?<![\w@.])@([A-Za-z0-9_][A-Za-z0-9_.\-]{0,63})")
MAIL_DELAY = 120  # seconds before an unread notification is mailed
MAIL_EVERY = 60
KEEP_DAYS = 90
EMAIL_CHOICES = ("all", "mentions", "off")


def mentioned(body: str) -> list[str]:
    """Usernames mentioned in a comment, in order, without repeats."""
    names: list[str] = []
    for m in MENTION.finditer(body):
        name = m.group(1).rstrip(".-")
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names


def excerpt(body: str, limit: int = 160) -> str:
    text = " ".join(body.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def record(conn: sqlite3.Connection, *, project_id: str, doc_id: str, comment_id: int, thread_id: int,
           actor_user_id: int | None, actor: str, body: str, kind: str = "comment") -> list[int]:
    """Create the notifications for a new comment, reply or suggestion. Returns the users notified."""
    notified: dict[int, str] = {}
    for name in (mentioned(body) if kind == "comment" else []):
        row = conn.execute(
            "SELECT u.id FROM users u JOIN project_members m ON m.user_id = u.id AND m.project_id = ?"
            " WHERE u.username = ? COLLATE NOCASE AND u.active = 1", (project_id, name)).fetchone()
        if row is not None and row["id"] != actor_user_id:
            notified.setdefault(row["id"], "mention")
    if comment_id != thread_id or kind == "suggestion-decided":
        # Everyone with an account who wrote in the thread hears about replies and decisions.
        for row in conn.execute("SELECT DISTINCT author_user_id FROM comments WHERE thread_id = ?"
                                " AND author_user_id IS NOT NULL", (thread_id,)):
            uid = row["author_user_id"]
            if uid != actor_user_id:
                notified.setdefault(uid, "suggestion" if kind == "suggestion-decided" else "reply")
    text = excerpt(body)
    for uid, what in notified.items():
        conn.execute("INSERT INTO notifications(user_id, doc_id, comment_id, thread_id, kind, actor, excerpt)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (uid, doc_id, comment_id, thread_id, what, actor, text))
    return list(notified)


def list_for(conn: sqlite3.Connection, user_id: int, limit: int = 50, unread_only: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT n.id, n.kind, n.actor, n.excerpt, n.created_at, n.read_at, n.thread_id, n.doc_id,"
        " d.path, d.project_id, p.name AS project FROM notifications n"
        " JOIN documents d ON d.id = n.doc_id AND d.deleted_at IS NULL"
        " JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL"
        " JOIN project_members m ON m.project_id = p.id AND m.user_id = n.user_id"
        f" WHERE n.user_id = ? {'AND n.read_at IS NULL' if unread_only else ''}"
        " ORDER BY n.id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM notifications n JOIN documents d ON d.id = n.doc_id AND d.deleted_at IS NULL"
        " JOIN project_members m ON m.project_id = d.project_id AND m.user_id = n.user_id"
        " WHERE n.user_id = ? AND n.read_at IS NULL", (user_id,)).fetchone()[0]


def mark_read(conn: sqlite3.Connection, user_id: int, *, ids: list[int] | None = None, doc_id: str | None = None,
              thread_id: int | None = None) -> int:
    sql = ("UPDATE notifications SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
           " WHERE user_id = ? AND read_at IS NULL")
    args: list = [user_id]
    if ids is not None:
        if not ids:
            return 0
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    if doc_id is not None:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    if thread_id is not None:
        sql += " AND thread_id = ?"
        args.append(thread_id)
    return conn.execute(sql, args).rowcount


def unread_threads(conn: sqlite3.Connection, user_id: int, doc_id: str) -> set[int]:
    return {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM notifications WHERE user_id = ? AND doc_id = ?"
                                       " AND read_at IS NULL AND thread_id IS NOT NULL", (user_id, doc_id))}


_SUBJECTS = {
    "mention": "{actor} mentioned you in {path}",
    "reply": "{actor} replied in {path}",
    "suggestion": "{actor} decided on a suggestion in {path}",
}


class Notifier:
    """Mails unread notifications in batches."""

    def __init__(self, app):
        self.app = app

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(MAIL_EVERY)
            try:
                await self.send_due()
            except Exception:  # noqa: BLE001
                log.exception("sending notifications failed")

    async def send_due(self, now: float | None = None) -> int:
        """Mail every person their unread notifications older than the delay. Returns mails sent."""
        if not self.app.mail.enabled:
            return 0
        now = now if now is not None else dt.datetime.now(dt.UTC).timestamp()
        cutoff = dt.datetime.fromtimestamp(now - MAIL_DELAY, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.999Z")

        def load(c):
            return [dict(r) for r in c.execute(
                "SELECT n.id, n.user_id, n.kind, n.actor, n.excerpt, n.thread_id, d.path, d.project_id,"
                " u.email, u.display_name, u.preferences FROM notifications n"
                " JOIN users u ON u.id = n.user_id AND u.active = 1"
                " JOIN documents d ON d.id = n.doc_id AND d.deleted_at IS NULL"
                " WHERE n.emailed_at IS NULL AND n.read_at IS NULL AND n.created_at <= ? ORDER BY n.id",
                (cutoff,))]

        rows = await self.app.db.read(load)
        if not rows:
            return 0
        by_user: dict[int, list[dict]] = defaultdict(list)
        for r in rows:
            by_user[r["user_id"]].append(r)
        sent = 0
        done: list[int] = []
        for items in by_user.values():
            done += [i["id"] for i in items]
            first = items[0]
            choice = _email_choice(first["preferences"])
            wanted = [i for i in items if choice == "all" or (choice == "mentions" and i["kind"] == "mention")]
            if not wanted or not first["email"]:
                continue
            subject, body = self.compose(first["display_name"], wanted)
            try:
                await self.app.mail.send(first["email"], subject, body)
                sent += 1
            except MailError as exc:
                log.warning("notification mail failed", extra=fields(user=first["user_id"], error=str(exc)))
        await self.app.db.write(lambda c: c.executemany(
            "UPDATE notifications SET emailed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            [(i,) for i in done]))
        return sent

    def compose(self, name: str, items: list[dict]) -> tuple[str, str]:
        from .pages.projects import doc_url

        base = self.app.cfg.server.public_url.rstrip("/")
        if len(items) == 1:
            i = items[0]
            subject = _SUBJECTS[i["kind"]].format(actor=i["actor"], path=i["path"])
        else:
            subject = f"{len(items)} new comments for you in DocWizard"
        lines = [f"Hello {name},", ""]
        for i in items:
            lines.append(_SUBJECTS[i["kind"]].format(actor=i["actor"], path=i["path"]) + ":")
            lines.append(f"  “{i['excerpt']}”")
            lines.append(f"  {base}{doc_url(i['project_id'], i['path'])}#comment-{i['thread_id']}")
            lines.append("")
        lines.append(f"Choose which mails you get in your settings: {base}/settings")
        return subject, "\n".join(lines) + "\n"

    async def prune(self) -> int:
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await self.app.db.write(lambda c: c.execute(
            "DELETE FROM notifications WHERE created_at < ?", (cutoff,)).rowcount)


def _email_choice(preferences: str | dict | None) -> str:
    import json

    if isinstance(preferences, str):
        try:
            preferences = json.loads(preferences)
        except ValueError:
            preferences = {}
    choice = (preferences or {}).get("notify_email", "all")
    return choice if choice in EMAIL_CHOICES else "all"
