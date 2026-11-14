-- Two-step login, account tokens and admin controls (ARCHITECTURE.md §13.2, §13.6).

-- The last TOTP step used, so a code can't be replayed.
ALTER TABLE users ADD COLUMN totp_last_counter INTEGER;
-- Set by an admin: the user must turn on two-step login before doing anything else.
ALTER TABLE users ADD COLUMN require_totp INTEGER NOT NULL DEFAULT 0;

CREATE TABLE recovery_codes (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash       TEXT NOT NULL,               -- sha256 of the normalized code
    used_at         REAL,
    PRIMARY KEY (user_id, code_hash)
);

-- Single-use password reset and invitation tokens, stored hashed, valid for an hour (reset)
-- or a week (invitation).
CREATE TABLE account_tokens (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT NOT NULL CHECK (purpose IN ('reset', 'invite')),
    expires_at      REAL NOT NULL,
    used_at         REAL,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX account_tokens_user ON account_tokens(user_id);
CREATE INDEX audit_log_at ON audit_log(at);
