-- SQLite keeps legacy auth tables untouched; Rust owns the activity ledger.
CREATE TABLE IF NOT EXISTS platform_session_activity (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS platform_session_activity_user_idx
    ON platform_session_activity (user_id, last_activity);
