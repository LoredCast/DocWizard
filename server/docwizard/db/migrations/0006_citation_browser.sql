-- The reference library browser (ARCHITECTURE.md §10.2, §10.4): Zotero collections and
-- attachments, Zotero desktop's local API, and Zotero file storage on WebDAV.

-- NULL: the Zotero Web API at api.zotero.org. Otherwise the base URL of Zotero
-- desktop's local API (http://localhost:23119/api), whose versions are local to one
-- Zotero installation, named by its Zotero-Server-ID.
ALTER TABLE citation_sources ADD COLUMN api_url TEXT;
ALTER TABLE citation_sources ADD COLUMN server_id TEXT;
-- Sealed {"url", "username", "password"} of the WebDAV folder Zotero syncs files to.
ALTER TABLE citation_sources ADD COLUMN storage_enc TEXT;

ALTER TABLE citation_items ADD COLUMN collections TEXT NOT NULL DEFAULT '[]';  -- JSON list of collection keys
ALTER TABLE citation_items ADD COLUMN added_at TEXT;                           -- Zotero's dateAdded
ALTER TABLE citation_items ADD COLUMN search_text TEXT NOT NULL DEFAULT '';    -- folded key, names, year, titles

CREATE TABLE citation_collections (
    source_id       INTEGER NOT NULL REFERENCES citation_sources(id) ON DELETE CASCADE,
    collection_key  TEXT NOT NULL,
    name            TEXT NOT NULL,
    parent_key      TEXT,
    PRIMARY KEY (source_id, collection_key)
);

CREATE TABLE citation_attachments (
    source_id       INTEGER NOT NULL REFERENCES citation_sources(id) ON DELETE CASCADE,
    attachment_key  TEXT NOT NULL,
    parent_key      TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    content_type    TEXT NOT NULL DEFAULT '',
    filename        TEXT NOT NULL DEFAULT '',
    link_mode       TEXT NOT NULL DEFAULT '',  -- imported_file | imported_url | linked_file | linked_url
    url             TEXT,
    md5             TEXT,
    PRIMARY KEY (source_id, attachment_key)
);
CREATE INDEX citation_attachments_parent ON citation_attachments(source_id, parent_key);

-- Fetch every library again, now with collections and attachments.
UPDATE citation_sources SET library_version = 0, last_sync_at = NULL;
