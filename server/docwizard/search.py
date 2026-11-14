"""Full-text search across the documents a user can read (§7.3), with SQLite FTS5.

The index is updated by the materializer, never in the collaboration commit
path, and caught up in the background at startup. Without FTS5 compiled into
SQLite, search is simply unavailable.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3

from .logs import fields

log = logging.getLogger("docwizard.search")

START, END = "\x02", "\x03"
_WORD = re.compile(r"[\w][\w'-]*", re.UNICODE)


def fts_query(text: str) -> str | None:
    """Turn what someone typed into a safe FTS5 query: all words must match, the last as a prefix."""
    words = _WORD.findall(text.lower())[:12]
    if not words:
        return None
    quoted = ['"' + w.replace('"', "") + '"' for w in words]
    quoted[-1] += "*"
    return " ".join(quoted)


def highlight(snippet: str) -> str:
    """Escape a snippet and turn the match markers into <mark> elements."""
    return html.escape(snippet).replace(START, "<mark>").replace(END, "</mark>")


class Search:
    def __init__(self, app):
        self.app = app
        self.available = False

    async def startup(self) -> None:
        def setup(c: sqlite3.Connection) -> bool:
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_search USING fts5("
                          "path, body, doc_id UNINDEXED, project_id UNINDEXED,"
                          " tokenize = 'unicode61 remove_diacritics 2')")
                c.execute("CREATE TABLE IF NOT EXISTS doc_search_state ("
                          "doc_id TEXT PRIMARY KEY, version INTEGER NOT NULL)")
                return True
            except sqlite3.OperationalError as exc:
                log.warning("full-text search unavailable", extra=fields(error=str(exc)))
                return False

        self.available = await self.app.db.write(setup)

    @staticmethod
    def _put(c: sqlite3.Connection, doc_id: str, project_id: str, path: str, text: str, version: int) -> None:
        c.execute("DELETE FROM doc_search WHERE doc_id = ?", (doc_id,))
        c.execute("INSERT INTO doc_search(path, body, doc_id, project_id) VALUES (?, ?, ?, ?)",
                  (path, text, doc_id, project_id))
        c.execute("INSERT INTO doc_search_state(doc_id, version) VALUES (?, ?)"
                  " ON CONFLICT(doc_id) DO UPDATE SET version = excluded.version", (doc_id, version))

    async def index(self, doc_id: str, project_id: str, path: str, text: str, version: int) -> None:
        if self.available:
            await self.app.db.write(lambda c: self._put(c, doc_id, project_id, path, text, version))

    async def catch_up(self) -> int:
        """Index documents changed while the index wasn't looking, and drop deleted ones."""
        if not self.available:
            return 0
        from .projects import service as svc

        def stale(c):
            c.execute("DELETE FROM doc_search WHERE doc_id NOT IN (SELECT id FROM documents WHERE deleted_at IS NULL)")
            c.execute("DELETE FROM doc_search_state WHERE doc_id NOT IN"
                      " (SELECT id FROM documents WHERE deleted_at IS NULL)")
            return [dict(r) for r in c.execute(
                "SELECT d.id, d.project_id, d.path FROM documents d LEFT JOIN doc_search_state s ON s.doc_id = d.id"
                " WHERE d.deleted_at IS NULL AND (s.version IS NULL OR s.version < d.version)").fetchall()]

        rows = await self.app.db.write(stale)
        for d in rows:
            state = await self.app.db.read(lambda c, doc_id=d["id"]: svc.load_state(c, doc_id))
            await self.index(d["id"], d["project_id"], d["path"], state.text, state.version)
        if rows:
            log.info("search index caught up", extra=fields(documents=len(rows)))
        return len(rows)

    async def query(self, user_id: int, text: str, limit: int = 50) -> list[dict]:
        q = fts_query(text)
        if not self.available or q is None:
            return []

        def run(c):
            try:
                return c.execute(
                    "SELECT s.doc_id, s.path, p.id AS project_id, p.name AS project_name,"
                    f" snippet(doc_search, 1, '{START}', '{END}', '…', 16) AS snip"
                    " FROM doc_search s JOIN projects p ON p.id = s.project_id AND p.deleted_at IS NULL"
                    " JOIN project_members m ON m.project_id = p.id AND m.user_id = ?"
                    " JOIN documents d ON d.id = s.doc_id AND d.deleted_at IS NULL"
                    " WHERE doc_search MATCH ? ORDER BY bm25(doc_search, 4.0, 1.0) LIMIT ?",
                    (user_id, q, limit)).fetchall()
            except sqlite3.OperationalError:
                return []

        rows = await self.app.db.read(run)
        return [{"doc": r["doc_id"], "path": r["path"], "project": r["project_id"], "projectName": r["project_name"],
                 "snippet": highlight(r["snip"])} for r in rows]
