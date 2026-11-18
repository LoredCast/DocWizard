-- Hosting for people the admin doesn't know (ARCHITECTURE.md §13.2, §7.7): confirmed email
-- addresses and email confirmation links.

-- When the account's email address was confirmed: through a mailed link, or by a login
-- provider that vouches for it. NULL: not confirmed (or no address).
ALTER TABLE users ADD COLUMN email_verified_at REAL;

-- Addresses that exist already were entered by an admin or came from a login provider.
UPDATE users SET email_verified_at = CAST(strftime('%s', 'now') AS REAL) WHERE email IS NOT NULL AND email != '';

CREATE INDEX users_email ON users(lower(email)) WHERE email IS NOT NULL;

-- SQLite can't change a CHECK constraint, so account_tokens is rebuilt to allow 'verify'.
-- pending_email: the address a verify link confirms (the account's address changes only once it
-- is confirmed), or the address a reset or invitation link was mailed to.
CREATE TEMP TABLE account_tokens_copy AS SELECT * FROM account_tokens;
DROP TABLE account_tokens;
CREATE TABLE account_tokens (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT NOT NULL CHECK (purpose IN ('reset', 'invite', 'verify')),
    expires_at      REAL NOT NULL,
    used_at         REAL,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    pending_email   TEXT
);
INSERT INTO account_tokens(token_hash, user_id, purpose, expires_at, used_at, created_by)
    SELECT token_hash, user_id, purpose, expires_at, used_at, created_by FROM account_tokens_copy;
DROP TABLE account_tokens_copy;
CREATE INDEX account_tokens_user ON account_tokens(user_id);
