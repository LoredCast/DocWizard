-- Document languages (ARCHITECTURE.md §9.8): the language a project's documents are written
-- in when their front matter doesn't say. NULL: the layout's default language.
ALTER TABLE projects ADD COLUMN lang TEXT;
