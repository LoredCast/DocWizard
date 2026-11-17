-- History (ARCHITECTURE.md §8.5): automatic snapshots are kept, thinned by age, and remember
-- who edited the text since the snapshot before them (a JSON list of display names).
ALTER TABLE doc_snapshots ADD COLUMN authors TEXT;
