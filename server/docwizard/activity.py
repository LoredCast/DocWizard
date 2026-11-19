"""Project activity (§8.12): a timeline of what happened in a project.

Events are recorded where they happen: files created, renamed and deleted, comments,
replies and suggestions and their decisions, outside changes and conflicts, sync runs,
versions, project-wide replacements and publishing. Edits are counted in the document's
room as they are committed and written here only when the room materializes, so typing
never pays for the timeline: one row per person and document covers a working session
(edits less than half an hour apart).

Rows keep the actor's name as it was, so the timeline reads the same after renames, and
are deleted after a year.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

EDIT_WINDOW = 30 * 60
KEEP_DAYS = 365
MAX_EXCERPT = 120

SOURCES = {"disk": "An outside edit", "sync:nextcloud": "Nextcloud", "sync:seafile": "Seafile",
           "sync:webdav": "WebDAV", "sync:folder": "The server folder", "sync:git": "Git"}


def excerpt(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_EXCERPT else text[:MAX_EXCERPT - 1].rstrip() + "…"


def record(c: sqlite3.Connection, project_id: str, kind: str, *, actor: str | int | None = None,
           actor_name: str | None = None, doc_id: str | None = None, path: str | None = None,
           detail: dict | None = None, count: int = 1, now: float | None = None) -> int:
    """Add one event; call it inside the write that makes the change."""
    now = time.time() if now is None else now
    if detail and "excerpt" in detail:
        detail = {**detail, "excerpt": excerpt(detail["excerpt"])}
    return c.execute(
        "INSERT INTO activity(project_id, doc_id, path, kind, actor, actor_name, detail_json, count, started_at, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, doc_id, path, kind, str(actor) if actor is not None else None, actor_name,
         json.dumps(detail, ensure_ascii=False) if detail else None, int(count), now, now)).lastrowid


def name_of(c: sqlite3.Connection, actor: str | None) -> str | None:
    """The display name behind an operation author: "12" is a user, "g3" a guest."""
    if not actor:
        return None
    if actor.isdigit():
        row = c.execute("SELECT display_name FROM users WHERE id = ?", (int(actor),)).fetchone()
        return row["display_name"] if row else None
    if actor.startswith("g") and actor[1:].isdigit():
        row = c.execute("SELECT display_name FROM guests WHERE id = ?", (int(actor[1:]),)).fetchone()
        return f"{row['display_name']} (guest)" if row else None
    return None


def add_edits(c: sqlite3.Connection, project_id: str, doc_id: str, path: str,
              tallies: dict[str, list[float]], now: float | None = None) -> None:
    """Fold a room's edit counts ({author: [changes, first_at, last_at]}) into the timeline:
    into the person's last edit row for the document when it is recent, else a new row."""
    now = time.time() if now is None else now
    for actor, (count, first, last) in tallies.items():
        row = c.execute("SELECT id FROM activity WHERE doc_id = ? AND actor = ? AND kind = 'edit' AND at >= ?"
                        " ORDER BY at DESC LIMIT 1", (doc_id, actor, first - EDIT_WINDOW)).fetchone()
        if row is not None:
            c.execute("UPDATE activity SET count = count + ?, at = ?, path = ? WHERE id = ?",
                      (int(count), last, path, row["id"]))
            continue
        c.execute(
            "INSERT INTO activity(project_id, doc_id, path, kind, actor, actor_name, count, started_at, at)"
            " VALUES (?, ?, ?, 'edit', ?, ?, ?, ?, ?)",
            (project_id, doc_id, path, actor, name_of(c, actor), int(count), first, last))


def list_for(c: sqlite3.Connection, project_id: str, *, before: int | None = None, limit: int = 50) -> list[dict]:
    """Newest first; *before* is the id of the last row of the previous page."""
    limit = max(1, min(int(limit), 200))
    if before is not None:
        rows = c.execute(
            "SELECT a.*, d.path AS current_path, d.deleted_at AS doc_deleted FROM activity a"
            " LEFT JOIN documents d ON d.id = a.doc_id"
            " WHERE a.project_id = ? AND (a.at, a.id) < (SELECT at, id FROM activity WHERE id = ?)"
            " ORDER BY a.at DESC, a.id DESC LIMIT ?", (project_id, before, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT a.*, d.path AS current_path, d.deleted_at AS doc_deleted FROM activity a"
            " LEFT JOIN documents d ON d.id = a.doc_id"
            " WHERE a.project_id = ? ORDER BY a.at DESC, a.id DESC LIMIT ?", (project_id, limit)).fetchall()
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
        except ValueError:
            detail = {}
        out.append({"id": r["id"], "kind": r["kind"], "actor": r["actor"], "actorName": r["actor_name"],
                    "docId": r["doc_id"], "path": r["path"],
                    # Where the document is now, to link to it; None once it is gone or in the trash.
                    "currentPath": r["current_path"] if r["current_path"] and not r["doc_deleted"] else None,
                    "detail": detail, "count": r["count"], "startedAt": r["started_at"], "at": r["at"]})
    return out


def prune(c: sqlite3.Connection, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return c.execute("DELETE FROM activity WHERE at < ?", (now - KEEP_DAYS * 86400,)).rowcount


def source_name(source: str | None, gettext) -> str:
    """"sync:nextcloud" → "Nextcloud", "offline:Jane" → "Offline edits by Jane"."""
    if not source:
        return gettext("Someone")
    if source.startswith("offline:"):
        return gettext("Offline edits by {name}", name=source[len("offline:"):])
    return gettext(SOURCES.get(source, source))


def describe(item: dict, gettext) -> str:
    """One sentence for an event, in the interface language (*gettext(message, **values)*)."""
    d = item.get("detail") or {}
    actor = item.get("actorName") or gettext("Someone")
    path = item.get("path") or ""
    kind = item["kind"]
    n = item.get("count") or 1
    if kind == "edit":
        what = gettext("1 change") if n == 1 else gettext("{n} changes", n=n)
        return gettext("{actor} edited {path} ({what})", actor=actor, path=path, what=what)
    if kind == "create":
        return gettext("{actor} created {path}", actor=actor, path=path)
    if kind == "upload":
        return gettext("{actor} uploaded {path}", actor=actor, path=path)
    if kind == "rename":
        return gettext("{actor} renamed {old} to {path}", actor=actor, old=d.get("from", ""), path=path)
    if kind == "delete":
        return gettext("{actor} moved {path} to the trash", actor=actor, path=path)
    if kind == "restore":
        return gettext("{actor} restored {path} from the trash", actor=actor, path=path)
    if kind == "comment":
        return gettext("{actor} commented on {path}: “{excerpt}”", actor=actor, path=path, excerpt=d.get("excerpt", ""))
    if kind == "reply":
        return gettext("{actor} replied in {path}: “{excerpt}”", actor=actor, path=path, excerpt=d.get("excerpt", ""))
    if kind == "suggestion":
        return gettext("{actor} suggested a change in {path}", actor=actor, path=path)
    if kind == "accept":
        return gettext("{actor} accepted a suggestion in {path}", actor=actor, path=path)
    if kind == "reject":
        return gettext("{actor} rejected a suggestion in {path}", actor=actor, path=path)
    if kind == "resolve":
        return gettext("{actor} resolved a comment in {path}", actor=actor, path=path)
    if kind == "report":
        return gettext("A reader reported a typo in {path}", path=path)
    if kind == "outside":
        return gettext("{source} changed {path}", source=source_name(d.get("source"), gettext), path=path)
    if kind == "conflict":
        source = source_name(d.get("source"), gettext)
        if n == 1:
            return gettext("1 conflict in {path} from {source}", path=path, source=source)
        return gettext("{n} conflicts in {path} from {source}", n=n, path=path, source=source)
    if kind == "sync":
        sent, received = d.get("sent", 0), d.get("received", 0)
        parts = []
        if sent:
            parts.append(gettext("1 file sent") if sent == 1 else gettext("{n} files sent", n=sent))
        if received:
            parts.append(gettext("1 file received") if received == 1 else gettext("{n} files received", n=received))
        if d.get("conflicts"):
            parts.append(gettext("{n} with conflicts", n=d["conflicts"]))
        return gettext("Sync with {provider}: {summary}", provider=source_name(d.get("source"), gettext),
                       summary=", ".join(parts) or gettext("checked"))
    if kind == "sync_error":
        return gettext("Sync with {provider} failed: {error}", provider=source_name(d.get("source"), gettext),
                       error=d.get("error", ""))
    if kind == "version":
        return gettext("{actor} saved the version “{label}” of {path}", actor=actor, label=d.get("label", ""),
                       path=path)
    if kind == "restore_version":
        return gettext("{actor} restored “{label}” in {path}", actor=actor, label=d.get("label", ""), path=path)
    if kind == "replace":
        docs = d.get("docs", 1)
        if docs == 1:
            return gettext("{actor} replaced “{query}” with “{replacement}” in {path} ({n} times)", actor=actor,
                           query=d.get("query", ""), replacement=d.get("replacement", ""), path=path, n=n)
        return gettext("{actor} replaced “{query}” with “{replacement}” in {docs} documents ({n} times)", actor=actor,
                       query=d.get("query", ""), replacement=d.get("replacement", ""), docs=docs, n=n)
    if kind == "publish":
        return gettext("{actor} published {path}", actor=actor, path=path)
    if kind == "unpublish":
        return gettext("{actor} unpublished {path}", actor=actor, path=path)
    return gettext("{actor}: {kind} {path}", actor=actor, kind=kind, path=path)


def iso(at: float) -> str:
    return dt.datetime.fromtimestamp(at, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def actor_of(req) -> tuple[str | None, str | None]:
    """The operation author ("12", "g3") and display name of a request's person."""
    if getattr(req, "user", None) is not None:
        return str(req.user.id), req.user.display_name
    if getattr(req, "guest", None) is not None:
        return f"g{req.guest.id}", req.guest.label
    return None, None


async def log(app, project_id: str, kind: str, req=None, **kw) -> None:
    """Record an event in its own write, after the change it describes. Never fails the request."""
    import logging

    if req is not None and "actor" not in kw:
        kw["actor"], kw["actor_name"] = actor_of(req)
    try:
        await app.db.write(lambda c: record(c, project_id, kind, **kw))
    except Exception:  # noqa: BLE001
        logging.getLogger("docwizard.activity").exception("recording activity failed")
