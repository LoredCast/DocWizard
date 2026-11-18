-- Sync with a folder on the server or a git repository (ARCHITECTURE.md §12.6, §12.7), next to
-- WebDAV. SQLite can't change a CHECK constraint, so sync_targets is rebuilt; sync_state goes
-- with it, because dropping sync_targets would cascade into it.
CREATE TEMP TABLE sync_state_copy AS SELECT * FROM sync_state;
CREATE TEMP TABLE sync_targets_copy AS SELECT * FROM sync_targets;
DROP TABLE sync_state;
DROP TABLE sync_targets;

CREATE TABLE sync_targets (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('nextcloud', 'seafile', 'webdav', 'folder', 'git')),
    base_url        TEXT NOT NULL,               -- WebDAV root, the folder's root, or the git remote
    remote_path     TEXT NOT NULL DEFAULT '',    -- folder below base_url, no leading or trailing slash
    branch          TEXT NOT NULL DEFAULT '',    -- git only
    author_name     TEXT NOT NULL DEFAULT '',    -- git only: who the commits are by
    author_email    TEXT NOT NULL DEFAULT '',
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

INSERT INTO sync_targets(id, project_id, kind, base_url, remote_path, username, secret_enc, poll_seconds, status,
                         last_error, last_sync_at, failures, next_poll_at, created_by, created_at)
    SELECT id, project_id, kind, base_url, remote_path, username, secret_enc, poll_seconds, status,
           last_error, last_sync_at, failures, next_poll_at, created_by, created_at FROM sync_targets_copy;
INSERT INTO sync_state SELECT * FROM sync_state_copy;
DROP TABLE sync_state_copy;
DROP TABLE sync_targets_copy;
