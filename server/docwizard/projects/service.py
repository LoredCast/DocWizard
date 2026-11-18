"""Projects, documents, assets, members and recent documents.

Functions here take a SQLite connection and run inside a writer unit or a
read transaction. They never touch the network and never import from the
HTTP layer.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

from ..collab.changeset import ChangeSet
from . import files


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class DocState:
    text: str
    version: int


# -- projects ------------------------------------------------------------

def role_of(conn: sqlite3.Connection, user_id: int, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT m.role FROM project_members m JOIN projects p ON p.id = m.project_id"
        " WHERE m.project_id = ? AND m.user_id = ? AND p.deleted_at IS NULL",
        (project_id, user_id),
    ).fetchone()
    return row[0] if row else None


def get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()


def list_projects(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.layout_id, p.updated_at, m.role,"
        " (SELECT count(*) FROM project_members WHERE project_id = p.id) AS members"
        " FROM projects p JOIN project_members m ON m.project_id = p.id AND m.user_id = ?"
        " WHERE p.deleted_at IS NULL ORDER BY p.name COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_project(conn: sqlite3.Connection, owner_id: int, name: str, layout_id: str) -> str:
    name = name.strip()[:120]
    if not name:
        raise ValueError("Give the project a name.")
    pid = new_id("p")
    conn.execute("INSERT INTO projects(id, owner_id, name, layout_id) VALUES (?, ?, ?, ?)",
                 (pid, owner_id, name, layout_id))
    conn.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?, ?, 'owner')", (pid, owner_id))
    return pid


def update_project(conn: sqlite3.Connection, project_id: str, *, name: str | None = None,
                   layout_id: str | None = None, layout_options: dict | None = None,
                   build_inputs: list[str] | None | bool = False, lang: str | None | bool = False) -> None:
    sets, params = [], []
    if lang is not False:
        sets.append("lang = ?")
        params.append(lang or None)
    if name is not None:
        name = name.strip()[:120]
        if not name:
            raise ValueError("Give the project a name.")
        sets.append("name = ?")
        params.append(name)
    if layout_id is not None:
        sets.append("layout_id = ?")
        params.append(layout_id)
    if layout_options is not None:
        sets.append("layout_options = ?")
        params.append(json.dumps(layout_options))
    if build_inputs is not False:
        sets.append("build_inputs = ?")
        params.append(json.dumps(build_inputs) if build_inputs else None)
    if not sets:
        return
    sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", (*params, project_id))


def delete_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("UPDATE projects SET deleted_at = ? WHERE id = ?", (now_iso(), project_id))


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                 (project_id,))


# -- documents -----------------------------------------------------------

def get_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,)).fetchone()


def get_document_by_path(conn: sqlite3.Connection, project_id: str, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                        (project_id, path)).fetchone()


def path_taken(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM documents WHERE project_id = ? AND path = ? AND deleted_at IS NULL"
        " UNION ALL SELECT 1 FROM assets WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
        (project_id, path, project_id, path)).fetchone())


def free_path(conn: sqlite3.Connection, project_id: str, path: str) -> str:
    """*path*, or the first of path-2, path-3, … (before the extension) that no file has."""
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        stem, dot, ext = path, "", ""
    candidate, n = path, 1
    while path_taken(conn, project_id, candidate):
        n += 1
        candidate = f"{stem}-{n}{dot}{ext}"
    return candidate


def create_document(conn: sqlite3.Connection, project_id: str, path: str, text: str = "",
                    line_ending: str = "lf") -> str:
    path = files.normalize(path)
    kind = files.kind_for(path)
    if kind is None:
        raise ValueError("Text documents end in .md, .tex, .bib, .json, .txt or another text extension.")
    if path_taken(conn, project_id, path):
        raise ValueError("A file with this name already exists.")
    doc_id = new_id("d")
    conn.execute("INSERT INTO documents(id, project_id, path, kind, line_ending) VALUES (?, ?, ?, ?, ?)",
                 (doc_id, project_id, path, kind, line_ending))
    conn.execute("INSERT INTO doc_snapshots(doc_id, version, text) VALUES (?, 0, ?)", (doc_id, text))
    touch_project(conn, project_id)
    return doc_id


def rename_document(conn: sqlite3.Connection, doc: sqlite3.Row, new_path: str) -> str:
    new_path = files.normalize(new_path)
    if files.kind_for(new_path) != doc["kind"] and not (files.kind_for(new_path) and doc["kind"] == "text"):
        raise ValueError("Keep the file extension of this kind of document.")
    if new_path != doc["path"] and path_taken(conn, doc["project_id"], new_path):
        raise ValueError("A file with this name already exists.")
    conn.execute("UPDATE documents SET path = ?, kind = ? WHERE id = ?",
                 (new_path, files.kind_for(new_path), doc["id"]))
    # A renamed chapter keeps its place in the build.
    row = conn.execute("SELECT build_inputs FROM projects WHERE id = ?", (doc["project_id"],)).fetchone()
    if row and row["build_inputs"] and new_path != doc["path"]:
        try:
            inputs = json.loads(row["build_inputs"])
        except ValueError:
            inputs = None
        if isinstance(inputs, list) and doc["path"] in inputs:
            conn.execute("UPDATE projects SET build_inputs = ? WHERE id = ?",
                         (json.dumps([new_path if p == doc["path"] else p for p in inputs]), doc["project_id"]))
    return new_path


def trash_document(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("UPDATE documents SET deleted_at = ? WHERE id = ?", (now_iso(), doc_id))


def load_state(conn: sqlite3.Connection, doc_id: str) -> DocState:
    """Rebuild a document's text from its latest snapshot plus later operations (§11.2)."""
    snap = conn.execute(
        "SELECT version, text FROM doc_snapshots WHERE doc_id = ? ORDER BY version DESC, id DESC LIMIT 1",
        (doc_id,)).fetchone()
    text, version = (snap["text"], snap["version"]) if snap else ("", 0)
    for row in conn.execute("SELECT version, changes FROM doc_ops WHERE doc_id = ? AND version >= ? ORDER BY version",
                            (doc_id, version)):
        if row["version"] != version:
            raise RuntimeError(f"operation log of {doc_id} has a gap at version {version}")
        text = ChangeSet.from_json(json.loads(row["changes"])).apply(text)
        version += 1
    return DocState(text, version)


def ops_since(conn: sqlite3.Connection, doc_id: str, version: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT version, client_id, changes FROM doc_ops WHERE doc_id = ? AND version >= ?"
                        " ORDER BY version", (doc_id, version)).fetchall()


def list_versions(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.version, s.label, s.created_at, u.display_name AS author, length(s.text) AS size"
        " FROM doc_snapshots s LEFT JOIN users u ON u.id = s.created_by"
        " WHERE s.doc_id = ? AND s.label IS NOT NULL ORDER BY s.id DESC", (doc_id,)).fetchall()
    return [dict(r) for r in rows]


# -- assets --------------------------------------------------------------

def get_asset(conn: sqlite3.Connection, project_id: str, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                        (project_id, path)).fetchone()


def put_asset(conn: sqlite3.Connection, project_id: str, path: str, digest: str, size: int,
              stat: tuple | None = None) -> None:
    if get_document_by_path(conn, project_id, path):
        raise ValueError("A text document with this name already exists.")
    existing = get_asset(conn, project_id, path)
    st = json.dumps(stat) if stat else None
    if existing:
        conn.execute("UPDATE assets SET sha256 = ?, size = ?, materialized_stat = ?,"
                     " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                     " WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                     (digest, size, st, project_id, path))
    else:
        conn.execute("INSERT INTO assets(project_id, path, sha256, size, materialized_stat) VALUES (?, ?, ?, ?, ?)",
                     (project_id, path, digest, size, st))
    touch_project(conn, project_id)


def trash_asset(conn: sqlite3.Connection, project_id: str, path: str) -> None:
    conn.execute("UPDATE assets SET deleted_at = ? WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                 (now_iso(), project_id, path))


def rename_asset(conn: sqlite3.Connection, project_id: str, path: str, new_path: str) -> str:
    new_path = files.normalize(new_path)
    if get_asset(conn, project_id, path) is None:
        raise ValueError("No such file.")
    if files.kind_for(new_path) is not None:
        raise ValueError("Keep the file extension of this kind of file.")
    if new_path != path and path_taken(conn, project_id, new_path):
        raise ValueError("A file with this name already exists.")
    conn.execute("UPDATE assets SET path = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                 " WHERE project_id = ? AND path = ? AND deleted_at IS NULL", (new_path, project_id, path))
    touch_project(conn, project_id)
    return new_path


# -- trash -----------------------------------------------------------------

def list_trash(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Trashed documents and files of a project, newest first."""
    rows = conn.execute(
        "SELECT 'document' AS type, id, path, deleted_at, json_extract(materialized_stat, '$[2]') AS size"
        " FROM documents WHERE project_id = :p AND deleted_at IS NOT NULL"
        " UNION ALL SELECT 'asset', NULL, path, deleted_at, size FROM assets"
        " WHERE project_id = :p AND deleted_at IS NOT NULL ORDER BY deleted_at DESC", {"p": project_id}).fetchall()
    return [dict(r) for r in rows]


def restore_document(conn: sqlite3.Connection, project_id: str, doc_id: str) -> str:
    row = conn.execute("SELECT path FROM documents WHERE id = ? AND project_id = ? AND deleted_at IS NOT NULL",
                       (doc_id, project_id)).fetchone()
    if row is None:
        raise ValueError("This file is no longer in the trash.")
    path = free_path(conn, project_id, row["path"])
    conn.execute("UPDATE documents SET deleted_at = NULL, path = ?, materialized_version = -1 WHERE id = ?",
                 (path, doc_id))
    touch_project(conn, project_id)
    return path


def restore_asset(conn: sqlite3.Connection, project_id: str, path: str, deleted_at: str) -> tuple[str, str]:
    row = conn.execute("SELECT rowid, sha256 FROM assets WHERE project_id = ? AND path = ? AND deleted_at = ?",
                       (project_id, path, deleted_at)).fetchone()
    if row is None:
        raise ValueError("This file is no longer in the trash.")
    new_path = free_path(conn, project_id, path)
    conn.execute("UPDATE assets SET deleted_at = NULL, path = ? WHERE rowid = ?", (new_path, row["rowid"]))
    touch_project(conn, project_id)
    return new_path, row["sha256"]


def empty_trash(conn: sqlite3.Connection, project_id: str) -> dict:
    """Delete a project's trashed documents and files for good. Blobs go with the daily collection."""
    docs = conn.execute("DELETE FROM documents WHERE project_id = ? AND deleted_at IS NOT NULL",
                        (project_id,)).rowcount
    assets = conn.execute("DELETE FROM assets WHERE project_id = ? AND deleted_at IS NOT NULL",
                          (project_id,)).rowcount
    return {"documents": docs, "assets": assets}


def purge_project(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Delete a project and everything in it for good. Returns the slugs of its publications."""
    slugs = [r[0] for r in conn.execute("SELECT slug FROM publications WHERE project_id = ?", (project_id,))]
    conn.execute("DELETE FROM publications WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return slugs


def trashed_projects(conn: sqlite3.Connection, owner_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, name, deleted_at FROM projects WHERE owner_id = ? AND deleted_at IS NOT NULL"
                        " ORDER BY deleted_at DESC", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def tree(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    docs = conn.execute(
        "SELECT id, path, kind, version, updated_at, materialized_stat FROM documents"
        " WHERE project_id = ? AND deleted_at IS NULL", (project_id,)).fetchall()
    assets = conn.execute("SELECT path, size, updated_at FROM assets WHERE project_id = ? AND deleted_at IS NULL",
                          (project_id,)).fetchall()
    out = []
    for d in docs:
        st = json.loads(d["materialized_stat"]) if d["materialized_stat"] else None
        out.append({"type": "document", "id": d["id"], "path": d["path"], "kind": d["kind"],
                    "version": d["version"], "size": st[2] if st else None, "updatedAt": d["updated_at"]})
    for a in assets:
        out.append({"type": "asset", "path": a["path"], "size": a["size"], "updatedAt": a["updated_at"]})
    out.sort(key=lambda e: (e["path"].count("/"), e["path"].lower()))
    return out


# -- storage (§7.7) ---------------------------------------------------------

def user_storage_bytes(conn: sqlite3.Connection, user_id: int) -> int:
    """Bytes the projects *user_id* owns take up, trashed projects and files included.

    Counted: every asset row (active and trashed), each document at its current size (the file
    on disk, or its latest snapshot before it was first written), and named versions. Rows the
    daily purge removed no longer count. Automatic history is not counted: it is thinned by age
    and nobody can delete it by hand.
    """
    row = conn.execute(
        "SELECT"
        " (SELECT COALESCE(SUM(a.size), 0) FROM assets a JOIN projects p ON p.id = a.project_id"
        "   WHERE p.owner_id = :u),"
        " (SELECT COALESCE(SUM(COALESCE(json_extract(d.materialized_stat, '$[2]'),"
        "   (SELECT length(CAST(s.text AS BLOB)) FROM doc_snapshots s WHERE s.doc_id = d.id"
        "    ORDER BY s.version DESC, s.id DESC LIMIT 1), 0)), 0)"
        "   FROM documents d JOIN projects p ON p.id = d.project_id WHERE p.owner_id = :u),"
        " (SELECT COALESCE(SUM(length(CAST(s.text AS BLOB))), 0) FROM doc_snapshots s"
        "   JOIN documents d ON d.id = s.doc_id JOIN projects p ON p.id = d.project_id"
        "   WHERE p.owner_id = :u AND s.label IS NOT NULL)",
        {"u": user_id}).fetchone()
    return int(row[0] + row[1] + row[2])


def total_storage_bytes(conn: sqlite3.Connection) -> int:
    """What every account's storage adds up to, counted as in user_storage_bytes (for /metrics)."""
    row = conn.execute(
        "SELECT (SELECT COALESCE(SUM(size), 0) FROM assets),"
        " (SELECT COALESCE(SUM(COALESCE(json_extract(d.materialized_stat, '$[2]'),"
        "   (SELECT length(CAST(s.text AS BLOB)) FROM doc_snapshots s WHERE s.doc_id = d.id"
        "    ORDER BY s.version DESC, s.id DESC LIMIT 1), 0)), 0) FROM documents d),"
        " (SELECT COALESCE(SUM(length(CAST(text AS BLOB))), 0) FROM doc_snapshots WHERE label IS NOT NULL)").fetchone()
    return int(row[0] + row[1] + row[2])


def project_owner(conn: sqlite3.Connection, project_id: str) -> int | None:
    row = conn.execute("SELECT owner_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row[0] if row else None


def count_owned_projects(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM projects WHERE owner_id = ? AND deleted_at IS NULL",
                        (user_id,)).fetchone()[0]


def count_files(conn: sqlite3.Connection, project_id: str) -> int:
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM documents WHERE project_id = ? AND deleted_at IS NULL)"
        " + (SELECT COUNT(*) FROM assets WHERE project_id = ? AND deleted_at IS NULL)",
        (project_id, project_id)).fetchone()[0]


# -- recent --------------------------------------------------------------

def touch_recent(conn: sqlite3.Connection, user_id: int, doc_id: str) -> None:
    conn.execute(
        "INSERT INTO recent(user_id, doc_id, opened_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id, doc_id) DO UPDATE SET opened_at = excluded.opened_at",
        (user_id, doc_id, now_iso()))


def set_pinned(conn: sqlite3.Connection, user_id: int, doc_id: str, pinned: bool) -> None:
    conn.execute("UPDATE recent SET pinned = ? WHERE user_id = ? AND doc_id = ?", (int(pinned), user_id, doc_id))


def list_recent(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT d.id, d.path, d.project_id, p.name AS project, r.opened_at, r.pinned, d.materialized_stat"
        " FROM recent r JOIN documents d ON d.id = r.doc_id AND d.deleted_at IS NULL"
        " JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL"
        " JOIN project_members m ON m.project_id = p.id AND m.user_id = r.user_id"
        " WHERE r.user_id = ? ORDER BY r.pinned DESC, r.opened_at DESC LIMIT ?",
        (user_id, limit)).fetchall()
    out = []
    for r in rows:
        st = json.loads(r["materialized_stat"]) if r["materialized_stat"] else None
        item = dict(r)
        item["size"] = st[2] if st else None
        out.append(item)
    return out


# -- members ---------------------------------------------------------------

def list_members(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT u.id, u.username, u.display_name, m.role FROM project_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.project_id = ? ORDER BY u.display_name COLLATE NOCASE", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def set_member(conn: sqlite3.Connection, project_id: str, username: str, role: str) -> None:
    if role not in ("editor", "commenter", "viewer"):
        raise ValueError("Choose editor, commenter or viewer.")
    row = conn.execute("SELECT id FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
    if row is None:
        raise ValueError("No user with this username.")
    current = role_of(conn, row["id"], project_id)
    if current == "owner":
        raise ValueError("The owner's role can't be changed.")
    conn.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?, ?, ?)"
                 " ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role",
                 (project_id, row["id"], role))


def remove_member(conn: sqlite3.Connection, project_id: str, user_id: int) -> None:
    if role_of(conn, user_id, project_id) == "owner":
        raise ValueError("The owner can't be removed.")
    conn.execute("DELETE FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, user_id))
