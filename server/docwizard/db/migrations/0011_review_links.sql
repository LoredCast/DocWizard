-- Review links (ARCHITECTURE.md §14.1): open documents in the review view, which shows the text
-- as it reads, without Markdown, with comments and suggestions at hand.
ALTER TABLE share_links ADD COLUMN review INTEGER NOT NULL DEFAULT 0;
