-- Share links, guests, comments and per-project sharing settings (§8.8, §13.5, §14).

CREATE TABLE share_links (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    token_hash      BLOB NOT NULL UNIQUE,
    role            TEXT NOT NULL CHECK (role IN ('editor', 'commenter', 'viewer')),
    label           TEXT,
    expires_at      REAL,
    password_hash   TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    revoked_at      TEXT
);
CREATE INDEX share_links_project ON share_links(project_id);

CREATE TABLE guests (
    id              INTEGER PRIMARY KEY,
    display_name    TEXT NOT NULL,
    share_link_id   INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

ALTER TABLE sessions ADD COLUMN guest_id INTEGER REFERENCES guests(id) ON DELETE CASCADE;
ALTER TABLE collab_clients ADD COLUMN guest_id INTEGER REFERENCES guests(id) ON DELETE CASCADE;

ALTER TABLE projects ADD COLUMN editors_can_share INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN editors_can_publish INTEGER NOT NULL DEFAULT 0;

-- A thread is its first comment (thread_id = id) plus replies. Only the first
-- comment carries the anchor: UTF-16 offsets at anchor_version.
CREATE TABLE comments (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    thread_id       INTEGER,
    author_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_guest_id INTEGER REFERENCES guests(id) ON DELETE SET NULL,
    author_name     TEXT NOT NULL,
    body            TEXT NOT NULL,
    quote           TEXT,
    anchor_from     INTEGER,
    anchor_to       INTEGER,
    anchor_version  INTEGER,
    orphaned        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT,
    resolved_at     TEXT,
    resolved_by     TEXT
);
CREATE INDEX comments_doc ON comments(doc_id, thread_id);
