-- Preserve existing account IDs and password hashes while transferring identity ownership to Rust.
DO $$
BEGIN
    IF to_regclass('public.auth_user') IS NOT NULL THEN
        INSERT INTO platform_users (id, username, email, first_name, last_name, is_active, is_staff, is_superuser, last_login, date_joined)
        SELECT id, username, COALESCE(email, ''), COALESCE(first_name, ''), COALESCE(last_name, ''),
               is_active, is_staff, is_superuser, last_login, date_joined
        FROM auth_user
        ON CONFLICT (id) DO UPDATE SET
            username = EXCLUDED.username,
            email = EXCLUDED.email,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            is_active = EXCLUDED.is_active,
            is_staff = EXCLUDED.is_staff,
            is_superuser = EXCLUDED.is_superuser,
            last_login = EXCLUDED.last_login;

        INSERT INTO platform_credentials (user_id, credential_type, credential_hash)
        SELECT id, 'password', password FROM auth_user
        ON CONFLICT (user_id, credential_type) DO UPDATE SET
            credential_hash = EXCLUDED.credential_hash,
            updated_at = CURRENT_TIMESTAMP;

        ALTER TABLE rust_sessions DROP CONSTRAINT IF EXISTS rust_sessions_user_id_fkey;
        ALTER TABLE rust_sessions ADD CONSTRAINT rust_sessions_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES platform_users(id) ON DELETE CASCADE;
        PERFORM setval(pg_get_serial_sequence('platform_users', 'id'),
                       GREATEST(COALESCE((SELECT MAX(id) FROM platform_users), 1), 1), true);
    END IF;
END $$;
