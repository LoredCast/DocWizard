-- Notifications (ARCHITECTURE.md §8.9): mentions in comments, replies to threads people wrote
-- in, and decisions on suggestions. Unread ones are mailed after a short delay.
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    comment_id      INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    thread_id       INTEGER,
    kind            TEXT NOT NULL CHECK (kind IN ('mention', 'reply', 'suggestion')),
    actor           TEXT NOT NULL,
    excerpt         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at         TEXT,
    emailed_at      TEXT
);
CREATE INDEX notifications_user ON notifications(user_id, read_at);
CREATE INDEX notifications_mail ON notifications(emailed_at, read_at);
