-- DocWizard initial schema (ARCHITECTURE.md §7.4).
-- Tables for later milestones (share links, comments, sync, citations from
-- Zotero, publications) arrive with their own migrations.

CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email           TEXT,
    display_name    TEXT NOT NULL,
    password_hash   TEXT,
    instance_role   TEXT NOT NULL DEFAULT 'user' CHECK (instance_role IN ('admin', 'user')),
    active          INTEGER NOT NULL DEFAULT 1,
    quota_bytes     INTEGER,
    preferences     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE sessions (
    token_hash      BLOB PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    csrf_token      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    user_agent      TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    owner_id        INTEGER NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    layout_id       TEXT NOT NULL DEFAULT 'default',
    layout_options  TEXT NOT NULL DEFAULT '{}',
    build_inputs    TEXT,
    preview_scope   TEXT NOT NULL DEFAULT 'build',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at      TEXT
);

CREATE TABLE project_members (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'commenter', 'viewer')),
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE documents (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path                  TEXT NOT NULL,
    kind                  TEXT NOT NULL CHECK (kind IN ('markdown', 'latex', 'bib', 'text')),
    version               INTEGER NOT NULL DEFAULT 0,
    snapshot_version      INTEGER NOT NULL DEFAULT 0,
    materialized_version  INTEGER NOT NULL DEFAULT -1,
    materialized_sha256   TEXT,
    pending_sha256        TEXT,
    materialized_stat     TEXT,
    line_ending           TEXT NOT NULL DEFAULT 'lf',
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT
);
CREATE UNIQUE INDEX documents_path ON documents(project_id, path) WHERE deleted_at IS NULL;

CREATE TABLE doc_ops (
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    client_id       TEXT NOT NULL,
    author          TEXT,
    changes         TEXT NOT NULL,
    merge_id        TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (doc_id, version)
) WITHOUT ROWID;
CREATE UNIQUE INDEX doc_ops_merge ON doc_ops(doc_id, merge_id) WHERE merge_id IS NOT NULL;

CREATE TABLE collab_clients (
    client_id       TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL
);

CREATE TABLE doc_snapshots (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    text            TEXT NOT NULL,
    label           TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX doc_snapshots_doc ON doc_snapshots(doc_id, version);

CREATE TABLE conflicts (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    base_text       TEXT NOT NULL,
    theirs_text     TEXT NOT NULL,
    anchor_from     INTEGER NOT NULL,
    anchor_to       INTEGER NOT NULL,
    anchor_version  INTEGER NOT NULL,
    version_label   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at     TEXT,
    resolved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE assets (
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path              TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    size              INTEGER NOT NULL,
    materialized_stat TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at        TEXT
);
CREATE UNIQUE INDEX assets_path ON assets(project_id, path) WHERE deleted_at IS NULL;

CREATE TABLE recent (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    opened_at       TEXT NOT NULL,
    pinned          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, doc_id)
);

CREATE TABLE settings (
    key             TEXT PRIMARY KEY,
    value_json      TEXT NOT NULL
);

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY,
    actor           TEXT,
    action          TEXT NOT NULL,
    target          TEXT,
    meta_json       TEXT NOT NULL DEFAULT '{}',
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
