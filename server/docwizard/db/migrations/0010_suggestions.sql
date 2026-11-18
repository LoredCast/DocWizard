-- Suggestions (ARCHITECTURE.md §8.10): a comment thread can propose replacement text for the
-- passage it is anchored to. Accepting applies it as an ordinary edit; accepted or rejected,
-- the thread is resolved and keeps the decision.
ALTER TABLE comments ADD COLUMN suggestion TEXT;
ALTER TABLE comments ADD COLUMN suggestion_state TEXT CHECK (suggestion_state IN ('accepted', 'rejected'));
