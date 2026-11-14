-- Integrations (ARCHITECTURE.md §10, §12, §13.4, §14.4): WebDAV sync, Zotero,
-- OIDC identities and publishing. Columns ending in _enc hold values sealed by
-- secretbox.py (AES-256-GCM).

CREATE TABLE sync_targets (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('nextcloud', 'seafile', 'webdav')),
    base_url        TEXT NOT NULL,               -- WebDAV root, without the folder
    remote_path     TEXT NOT NULL DEFAULT '',    -- folder below base_url, no leading or trailing slash
    username        TEXT NOT NULL DEFAULT '',
    secret_enc      TEXT,
    poll_seconds    INTEGER NOT NULL DEFAULT 60,
    status          TEXT NOT NULL DEFAULT 'new', -- new | ok | error | paused
    last_error      TEXT,
    last_sync_at    REAL,
    failures        INTEGER NOT NULL DEFAULT 0,
    next_poll_at    REAL NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The base of every synced file: its content (a blob) and remote ETag at the last
-- successful transfer. Folders are kept too (path ends in '/') so unchanged
-- subtrees can be skipped by their ETag.
CREATE TABLE sync_state (
    target_id       INTEGER NOT NULL REFERENCES sync_targets(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    item_id         TEXT,                        -- document id, or NULL for assets and folders
    base_sha256     TEXT,
    remote_etag     TEXT,
    remote_fileid   TEXT,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (target_id, path)
);

CREATE TABLE citation_sources (
    id              INTEGER PRIMARY KEY,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('zotero_web')),
    library_type    TEXT NOT NULL CHECK (library_type IN ('user', 'group')),
    library_id      TEXT NOT NULL,
    collection      TEXT,
    config_enc      TEXT NOT NULL,               -- {"api_key": ...}
    library_version INTEGER NOT NULL DEFAULT 0,
    last_sync_at    REAL,
    status          TEXT NOT NULL DEFAULT 'new',
    last_error      TEXT,
    UNIQUE (owner_id, name)
);

CREATE TABLE citation_items (
    source_id       INTEGER NOT NULL REFERENCES citation_sources(id) ON DELETE CASCADE,
    item_key        TEXT NOT NULL,
    csl_json        TEXT NOT NULL,
    PRIMARY KEY (source_id, item_key)
);

-- Generated keys never change once assigned (§10.3).
CREATE TABLE citation_keys (
    source_id       INTEGER NOT NULL REFERENCES citation_sources(id) ON DELETE CASCADE,
    item_key        TEXT NOT NULL,
    citation_key    TEXT NOT NULL,
    PRIMARY KEY (source_id, item_key),
    UNIQUE (source_id, citation_key)
);

CREATE TABLE identities (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    email           TEXT,
    last_login_at   REAL,
    UNIQUE (issuer, subject)
);

ALTER TABLE users ADD COLUMN totp_secret_enc TEXT;
-- The OIDC provider a session logged in with, for RP-initiated logout.
ALTER TABLE sessions ADD COLUMN oidc_provider TEXT;

CREATE TABLE publications (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL UNIQUE,
    layout_id       TEXT,
    version         INTEGER,
    visibility      TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'unlisted')),
    auto_update     INTEGER NOT NULL DEFAULT 0,
    with_pdf        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'new',
    last_error      TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at      REAL
);
