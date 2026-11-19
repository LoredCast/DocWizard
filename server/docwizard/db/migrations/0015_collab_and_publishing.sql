-- Project activity, spelling dictionaries and living editions (ARCHITECTURE.md §8.12, §6.8, §14.4).

-- What happened in a project, newest first: edits (coalesced per person and document),
-- files created, renamed and deleted, comments and suggestions, sync runs and conflicts,
-- versions, replacements and publishing. Kept for a year.
CREATE TABLE activity (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id          TEXT REFERENCES documents(id) ON DELETE SET NULL,
    path            TEXT,
    kind            TEXT NOT NULL,
    actor           TEXT,           -- "12" (a user), "g3" (a guest), "system:<what>" or NULL
    actor_name      TEXT,
    detail_json     TEXT,
    count           INTEGER NOT NULL DEFAULT 1,
    started_at      REAL NOT NULL,
    at              REAL NOT NULL
);
CREATE INDEX activity_project ON activity(project_id, at DESC);
CREATE INDEX activity_recent_edit ON activity(doc_id, actor, at DESC) WHERE kind = 'edit';

-- Words the spelling checkers accept: for everyone in a project, and for one person everywhere.
CREATE TABLE project_words (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    word            TEXT NOT NULL,
    added_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (project_id, word)
) WITHOUT ROWID;

CREATE TABLE user_words (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    word            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, word)
) WITHOUT ROWID;

-- Publications: a whole multi-chapter build as a small site (edition), anonymized for
-- double-blind review, behind a password, with reader typo reports, and more downloads.
ALTER TABLE publications ADD COLUMN edition INTEGER NOT NULL DEFAULT 0;
ALTER TABLE publications ADD COLUMN anonymize INTEGER NOT NULL DEFAULT 0;
ALTER TABLE publications ADD COLUMN password_hash TEXT;
ALTER TABLE publications ADD COLUMN reports INTEGER NOT NULL DEFAULT 0;
ALTER TABLE publications ADD COLUMN with_epub INTEGER NOT NULL DEFAULT 0;
ALTER TABLE publications ADD COLUMN with_docx INTEGER NOT NULL DEFAULT 0;
