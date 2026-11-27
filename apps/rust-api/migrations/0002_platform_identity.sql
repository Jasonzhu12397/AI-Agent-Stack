-- Preserve existing account IDs and password hashes while transferring identity ownership to Rust.
DO $$
BEGIN
    IF to_regclass('public.auth_user') IS NOT NULL THEN
        INSERT INTO platform_users (id, username, email, first_name, last_name, is_active, is_staff, is_superuser, last_login, date_joined)
        SELECT id, username, COALESCE(email, ''), COALESCE(first_name, ''), COALESCE(last_name, ''),
               is_active, is_staff, is_superuser, last_login, date_joined
        FROM auth_user
        ON CONFLICT (id) DO NOTHING;

        INSERT INTO platform_credentials (user_id, credential_type, credential_hash)
        SELECT id, 'password', password FROM auth_user
        ON CONFLICT (user_id, credential_type) DO NOTHING;

        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'rust_sessions'::regclass
              AND conname = 'rust_sessions_user_id_fkey'
              AND confrelid = 'auth_user'::regclass
        ) THEN
            ALTER TABLE rust_sessions DROP CONSTRAINT rust_sessions_user_id_fkey;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'rust_sessions'::regclass
              AND conname = 'rust_sessions_user_id_fkey'
              AND confrelid = 'platform_users'::regclass
        ) THEN
            ALTER TABLE rust_sessions ADD CONSTRAINT rust_sessions_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES platform_users(id) ON DELETE CASCADE;
        END IF;
        PERFORM setval(pg_get_serial_sequence('platform_users', 'id'),
                       GREATEST(COALESCE((SELECT MAX(id) FROM platform_users), 1), 1), true);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS platform_session_activity (
    session_key VARCHAR(128) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS platform_session_activity_user_idx
    ON platform_session_activity (user_id, last_activity);
