-- Layouts made in the layout editor (ARCHITECTURE.md §9.3). A custom layout is a manifest that
-- extends another layout and changes its design settings; templates and files come from the parent.
CREATE TABLE custom_layouts (
    id              TEXT PRIMARY KEY,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    manifest_json   TEXT NOT NULL,
    shared          INTEGER NOT NULL DEFAULT 1,   -- listed for everyone on the instance, not only the owner
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX custom_layouts_owner ON custom_layouts(owner_id);
