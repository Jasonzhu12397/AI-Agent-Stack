-- Rust API owns opaque session tokens. Existing browser sessions are intentionally invalidated.
-- User records and tenant/project foreign keys remain untouched.
CREATE TABLE IF NOT EXISTS rust_sessions (
    token_hash CHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS rust_sessions_user_expires_idx
    ON rust_sessions (user_id, expires_at);

CREATE INDEX IF NOT EXISTS rust_sessions_expires_idx
    ON rust_sessions (expires_at);
