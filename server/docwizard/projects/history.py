"""Document history (§8.5): thinning automatic snapshots, who edited, and comparing versions.

Automatic snapshots used to be dropped as soon as a newer one existed, so the only way back
to older text was a named version. Now they are kept and thinned by age: every snapshot of
the last hour, one per hour for two days, one per day for 60 days and one per week after
that. The snapshot kept for a period is its last one, and it inherits the authors of the
snapshots dropped before it, so "who edited" stays complete.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import sqlite3

HOUR = 3600
DAY = 86400
# (age up to, bucket size): snapshots older than the last limit fall into weekly buckets.
TIERS = ((HOUR, 0), (2 * DAY, HOUR), (60 * DAY, DAY))
WEEK = 7 * DAY


def _ts(created_at: str) -> float:
    return dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()


def bucket(age: float, ts: float) -> tuple[int, int] | None:
    """The thinning bucket of a snapshot taken at *ts*, *age* seconds ago; None keeps it."""
    for limit, size in TIERS:
        if age <= limit:
            return None if size == 0 else (size, int(ts // size))
    return WEEK, int(ts // WEEK)


def plan_thinning(rows: list[dict], now: float) -> tuple[list[int], dict[int, list[str]]]:
    """Which automatic snapshots to drop, and the merged authors of the ones that stay.

    *rows* are a document's automatic snapshots in version order, each with ``id``,
    ``created_at`` and ``authors`` (a list). The newest is always kept: documents load from it.
    """
    drop: list[int] = []
    merged: dict[int, list[str]] = {}
    pending: list[str] = []  # authors of dropped snapshots, carried to the next kept one
    for i, row in enumerate(rows):
        ts = _ts(row["created_at"])
        key = bucket(now - ts, ts)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        same_as_next = False
        if key is not None and nxt is not None:
            nts = _ts(nxt["created_at"])
            same_as_next = bucket(now - nts, nts) == key
        if same_as_next:
            drop.append(row["id"])
            pending += [a for a in row["authors"] if a not in pending]
        elif pending:
            names = list(pending) + [a for a in row["authors"] if a not in pending]
            if names != row["authors"]:
                merged[row["id"]] = names
            pending = []
    return drop, merged


def thin(conn: sqlite3.Connection, doc_id: str, now: float) -> int:
    rows = [{"id": r["id"], "created_at": r["created_at"], "authors": _authors(r["authors"])}
            for r in conn.execute("SELECT id, created_at, authors FROM doc_snapshots WHERE doc_id = ?"
                                  " AND label IS NULL ORDER BY version, id", (doc_id,))]
    drop, merged = plan_thinning(rows, now)
    for sid, names in merged.items():
        conn.execute("UPDATE doc_snapshots SET authors = ? WHERE id = ?", (json.dumps(names), sid))
    conn.executemany("DELETE FROM doc_snapshots WHERE id = ?", [(i,) for i in drop])
    return len(drop)


def _authors(value: str | None) -> list[str]:
    try:
        names = json.loads(value) if value else []
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def authors_since(conn: sqlite3.Connection, doc_id: str, since: int, until: int) -> list[str]:
    """Display names of the people whose edits are between two versions, in order of first edit."""
    rows = conn.execute("SELECT author, client_id, min(version) AS first FROM doc_ops WHERE doc_id = ? AND version >= ?"
                        " AND version < ? GROUP BY author, client_id ORDER BY first", (doc_id, since, until)).fetchall()
    names: list[str] = []
    for r in rows:
        name = _author_name(conn, r["author"], r["client_id"])
        if name and name not in names:
            names.append(name)
    return names


_SYSTEM = {"disk": "Outside edit", "upload": "Upload", "restore": "Restore"}


def _author_name(conn: sqlite3.Connection, author: str | None, client_id: str) -> str | None:
    if author and author.isdigit():
        row = conn.execute("SELECT display_name FROM users WHERE id = ?", (int(author),)).fetchone()
        return row["display_name"] if row else None
    if author and author.startswith("g") and author[1:].isdigit():
        row = conn.execute("SELECT display_name FROM guests WHERE id = ?", (int(author[1:]),)).fetchone()
        return f"{row['display_name']} (guest)" if row else "Guest"
    if client_id.startswith("system:"):
        source = client_id.split(":", 1)[1]
        return _SYSTEM.get(source, source.split(":", 1)[0].capitalize() if source else None)
    return None


def list_history(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    """Automatic snapshots, newest first."""
    rows = conn.execute("SELECT id, version, created_at, authors, length(text) AS size FROM doc_snapshots"
                        " WHERE doc_id = ? AND label IS NULL ORDER BY version DESC, id DESC", (doc_id,)).fetchall()
    return [{"id": r["id"], "version": r["version"], "created_at": r["created_at"], "authors": _authors(r["authors"]),
             "size": r["size"]} for r in rows]


# -- comparing -------------------------------------------------------------------

_TOKEN = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def _is_word(token: str) -> bool:
    return token[:1].isalnum() or token[:1] == "_"


def _words(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _push(out: list[dict], op: str, text: str) -> None:
    if not text:
        return
    if out and out[-1]["op"] == op:
        out[-1]["text"] += text
    else:
        out.append({"op": op, "text": text})


def compare(old: str, new: str, context: int = 2) -> dict:
    """A word-level comparison of two texts, for reading.

    Returns ``{"segments": [...], "added": words, "removed": words}``. A segment is
    ``{"op": "eq" | "ins" | "del", "text": ...}`` or ``{"op": "skip", "lines": n}`` for unchanged
    lines left out, keeping *context* lines around every change.
    """
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out: list[dict] = []
    added = removed = 0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    opcodes = sm.get_opcodes()
    for n, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            lines = a[i1:i2]
            first, last = n == 0, n == len(opcodes) - 1
            head = 0 if first else context
            tail = 0 if last else context
            if len(lines) > head + tail + 1:
                _push(out, "eq", "".join(lines[:head]))
                out.append({"op": "skip", "lines": len(lines) - head - tail})
                _push(out, "eq", "".join(lines[len(lines) - tail:]))
            else:
                _push(out, "eq", "".join(lines))
            continue
        old_words, new_words = _words("".join(a[i1:i2])), _words("".join(b[j1:j2]))
        inner = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
        for t, x1, x2, y1, y2 in inner.get_opcodes():
            if t == "equal":
                _push(out, "eq", "".join(old_words[x1:x2]))
                continue
            if x2 > x1:
                _push(out, "del", "".join(old_words[x1:x2]))
                removed += sum(1 for w in old_words[x1:x2] if _is_word(w))
            if y2 > y1:
                _push(out, "ins", "".join(new_words[y1:y2]))
                added += sum(1 for w in new_words[y1:y2] if _is_word(w))
    return {"segments": out, "added": added, "removed": removed}
