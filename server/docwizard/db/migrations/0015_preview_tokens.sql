-- Capability tokens for HTML previews on the preview origin (ARCHITECTURE.md §6.4, §9.2). Kept in the
-- database rather than the app's memory, so previews keep working across a restart and any process
-- that serves the preview origin can check them. Only the token's SHA-256 is stored.
CREATE TABLE preview_tokens (
    token_hash      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conn_key        TEXT NOT NULL,
    expires_at      REAL NOT NULL
);
CREATE INDEX preview_tokens_conn ON preview_tokens(conn_key);
CREATE INDEX preview_tokens_expires ON preview_tokens(expires_at);
