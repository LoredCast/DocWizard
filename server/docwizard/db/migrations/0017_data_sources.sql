-- Data sheets (.csv, .tsv) can be kept up to date from an HTTPS address (§11.3a).
CREATE TABLE data_sources (
    doc_id        TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    set_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    refreshed_at  TEXT,
    status        TEXT,
    etag          TEXT
);
