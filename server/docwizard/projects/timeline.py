"""The drafting timeline (§8.5): a document's history as points to scrub through, and its text at
any of them with what changed since the point before, and who wrote it.

The points are the snapshots that history keeps (thinned by age) and, where the operations are
still kept (7 days by default), every pause in one person's typing: operations by the same author
less than BURST seconds apart make one point. The text at a point is the newest snapshot at or
before it with the operations after it applied; the passages written since the previous point come
from those operations, each with its author, or, where the operations are gone, from comparing the
two texts.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import sqlite3

from ..collab.changeset import ChangeSet, InvalidChange, to_u16_offset
from .history import _authors, _author_name, _words

BURST = 5.0  # seconds between one person's operations that still count as one point (a pause in typing)
MAX_POINTS = 600  # more are merged, oldest first, into larger steps


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Names:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.cache: dict[tuple, str | None] = {}

    def __call__(self, author: str | None, client_id: str) -> str:
        key = (author, client_id if client_id.startswith("system:") else "")
        if key not in self.cache:
            self.cache[key] = _author_name(self.conn, author, client_id)
        return self.cache[key] or "Unknown"


def points(conn: sqlite3.Connection, doc_id: str, current_version: int) -> list[dict]:
    """The timeline's points in version order: ``{version, at, authors, label?, kind}``."""
    names = _Names(conn)
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT version, created_at, authors, label FROM doc_snapshots WHERE doc_id = ?"
                          " ORDER BY version, id", (doc_id,)):
        p = out.setdefault(r["version"], {"version": r["version"], "at": r["created_at"], "authors": [],
                                          "kind": "snapshot"})
        for a in _authors(r["authors"]):
            if a not in p["authors"]:
                p["authors"].append(a)
        if r["label"]:
            p["label"] = r["label"]
    steps: list[dict] = []
    for r in conn.execute("SELECT version, client_id, author, created_at FROM doc_ops WHERE doc_id = ?"
                          " ORDER BY version", (doc_id,)):
        who = names(r["author"], r["client_id"])
        last = steps[-1] if steps else None
        if last and last["authors"] == [who] and last["version"] == r["version"] \
                and r["created_at"] - last["ts"] < BURST:
            last["version"] = r["version"] + 1
            last["ts"] = r["created_at"]
        else:
            steps.append({"version": r["version"] + 1, "ts": r["created_at"], "authors": [who], "kind": "edit"})
    for s in steps:
        p = out.get(s["version"])
        if p is None:
            out[s["version"]] = {"version": s["version"], "at": _iso(s.pop("ts")), "authors": s["authors"],
                                 "kind": "edit"}
        else:
            p["authors"] += [a for a in s["authors"] if a not in p["authors"]]
    ordered = [out[v] for v in sorted(out) if v <= current_version]
    if not ordered or ordered[-1]["version"] != current_version:
        ordered.append({"version": current_version, "at": _iso(dt.datetime.now(dt.UTC).timestamp()),
                        "authors": [], "kind": "now"})
    return _merge_oldest(ordered)


def _merge_oldest(pts: list[dict]) -> list[dict]:
    """At most MAX_POINTS: neighbouring plain edit points merge, oldest first; labels stay."""
    if len(pts) <= MAX_POINTS:
        return pts
    out: list[dict] = []
    excess = len(pts) - MAX_POINTS
    for p in pts:
        if excess > 0 and out and out[-1].get("kind") == "edit" and p.get("kind") == "edit":
            # The earlier point disappears into this one.
            prev = out.pop()
            p = {**p, "authors": prev["authors"] + [a for a in p["authors"] if a not in prev["authors"]]}
            excess -= 1
        out.append(p)
    return out


def _snapshot_before(conn, doc_id: str, version: int):
    return conn.execute("SELECT version, text, authors FROM doc_snapshots WHERE doc_id = ? AND version <= ?"
                        " ORDER BY version DESC, id DESC LIMIT 1", (doc_id, version)).fetchone()


def _ops(conn, doc_id: str, start: int, end: int) -> list | None:
    """The operations from *start* to *end*, or None when some of them are gone."""
    if end <= start:
        return []
    rows = conn.execute("SELECT version, client_id, author, changes FROM doc_ops WHERE doc_id = ?"
                        " AND version >= ? AND version < ? ORDER BY version", (doc_id, start, end)).fetchall()
    if len(rows) != end - start:
        return None
    return rows


def text_at(conn: sqlite3.Connection, doc_id: str, version: int, since: int | None = None,
            current: tuple[int, str] | None = None) -> dict | None:
    """The text at *version* and the passages written since *since*, as UTF-16 ranges of that text
    with their author: ``{version, text, marks: [{from, to, author}], exact}``.

    *current* is the room's ``(version, text)``, used for the newest point. ``exact`` is false when
    the operations are gone and the marks come from comparing texts.
    """
    names = _Names(conn)
    if current and version >= current[0]:
        version, text_now = current
    else:
        text_now = None
    base = _snapshot_before(conn, doc_id, version)
    if since is not None:
        since = min(since, version)
        start = _snapshot_before(conn, doc_id, since)
    else:
        start = None
    # Replay from the snapshot before *since* when the operations from there are all kept.
    if start is not None:
        ops = _ops(conn, doc_id, start["version"], version)
        if ops is not None:
            text, marks = start["text"], []
            try:
                for r in ops:
                    cs = ChangeSet.from_json(json.loads(r["changes"]))
                    if r["version"] >= since:
                        marks = _map_marks(marks, cs)
                        who = names(r["author"], r["client_id"])
                        for _fa, _ta, fb, tb, _ins in cs.iter_changes():
                            if tb > fb:
                                marks.append({"from": fb, "to": tb, "author": who})
                    text = cs.apply(text)
            except (InvalidChange, ValueError):
                pass
            else:
                return {"version": version, "text": text, "marks": _join(marks), "exact": True}
    if base is None:
        if text_now is None:
            return None
        text, at = text_now, version
    else:
        ops = _ops(conn, doc_id, base["version"], version)
        text, at = base["text"], base["version"]
        if ops is not None:
            try:
                for r in ops:
                    text = ChangeSet.from_json(json.loads(r["changes"])).apply(text)
                at = version
            except (InvalidChange, ValueError):
                text, at = base["text"], base["version"]
        elif text_now is not None:
            text, at = text_now, version
    marks: list[dict] = []
    if since is not None and start is not None:
        older = text_at(conn, doc_id, since, None, current)
        if older is not None:
            who = ", ".join(_authors(base["authors"]) if base is not None else []) or None
            marks = [{"from": a, "to": b, "author": who} for a, b in inserted(older["text"], text)]
    return {"version": at, "text": text, "marks": marks, "exact": False}


def _map_marks(marks: list[dict], cs: ChangeSet) -> list[dict]:
    out = []
    for m in marks:
        a, b = cs.map_pos(m["from"], 1), cs.map_pos(m["to"], -1)
        if b > a:
            out.append({**m, "from": a, "to": b})
    return out


def _join(marks: list[dict]) -> list[dict]:
    marks = sorted(marks, key=lambda m: (m["from"], m["to"]))
    out: list[dict] = []
    for m in marks:
        if out and out[-1]["author"] == m["author"] and m["from"] <= out[-1]["to"]:
            out[-1]["to"] = max(out[-1]["to"], m["to"])
        else:
            out.append(dict(m))
    return out


def inserted(old: str, new: str) -> list[tuple[int, int]]:
    """Word-level passages of *new* that aren't in *old*, as UTF-16 ranges of *new*."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out: list[tuple[int, int]] = []
    line_start = [0]
    for line in b:
        line_start.append(line_start[-1] + len(line))
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            old_words = _words("".join(a[_i1:_i2])) if tag == "replace" else []
            new_words = _words("".join(b[j1:j2]))
            pos = line_start[j1]
            offsets = []
            for w in new_words:
                offsets.append(pos)
                pos += len(w)
            for t, _x1, _x2, y1, y2 in difflib.SequenceMatcher(None, old_words, new_words,
                                                               autojunk=False).get_opcodes():
                if t in ("insert", "replace") and y2 > y1:
                    s, e = offsets[y1], offsets[y2 - 1] + len(new_words[y2 - 1])
                    while s < e and new[s].isspace():
                        s += 1
                    while e > s and new[e - 1].isspace():
                        e -= 1
                    if e > s:
                        out.append((to_u16_offset(new, s), to_u16_offset(new, e)))
    return out


def contributions(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    """Characters written and removed per person over the kept operations, most first."""
    names = _Names(conn)
    totals: dict[str, list[int]] = {}
    for r in conn.execute("SELECT client_id, author, changes FROM doc_ops WHERE doc_id = ?", (doc_id,)):
        try:
            cs = ChangeSet.from_json(json.loads(r["changes"]))
        except (InvalidChange, ValueError):
            continue
        t = totals.setdefault(names(r["author"], r["client_id"]), [0, 0])
        for fa, ta, fb, tb, _ in cs.iter_changes():
            t[0] += tb - fb
            t[1] += ta - fa
    return [{"name": n, "inserted": i, "deleted": d}
            for n, (i, d) in sorted(totals.items(), key=lambda kv: -kv[1][0])]
