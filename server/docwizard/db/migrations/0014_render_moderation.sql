-- Render limits per account and publication takedowns (ARCHITECTURE.md §9.2, §14.4).

-- LaTeX builds for the projects this account owns. NULL: the server's [render] allow_latex.
ALTER TABLE users ADD COLUMN allow_latex INTEGER;
-- The time limit for renders of the projects this account owns. NULL: [render] timeout_seconds.
ALTER TABLE users ADD COLUMN render_timeout_seconds INTEGER;

-- Addresses an admin took a publication down from: nobody can publish there again.
CREATE TABLE taken_down_slugs (
    slug            TEXT PRIMARY KEY,
    reason          TEXT,
    taken_down_at   REAL NOT NULL,
    taken_down_by   INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- /readyz writes here to check that the database takes writes.
CREATE TABLE health_probe (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    checked_at      REAL NOT NULL
);
