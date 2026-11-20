-- Organizing the home page (ARCHITECTURE.md §6.2): each member keeps their own tags for a project
-- and can archive it for themselves, without changing what the other members see.

-- The member's tags for the project, as a JSON list of strings. NULL: none.
ALTER TABLE project_members ADD COLUMN tags TEXT;
-- When the member archived the project. NULL: it is listed on their home page.
ALTER TABLE project_members ADD COLUMN archived_at REAL;
