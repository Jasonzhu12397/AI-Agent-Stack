CREATE TABLE IF NOT EXISTS platform_user_profiles (
    id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE REFERENCES platform_users(id) ON DELETE CASCADE,
    display_name VARCHAR(160) NOT NULL DEFAULT '',
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    locale VARCHAR(16) NOT NULL DEFAULT 'zh-hans',
    updated_by_id BIGINT NULL REFERENCES platform_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS platform_user_profiles_updated_at_idx
    ON platform_user_profiles (updated_at);
