-- Preserve existing account IDs and password hashes while transferring identity ownership to Rust.
DO $$
DECLARE
    fk RECORD;
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

        FOR fk IN
            SELECT conrelid, conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE contype = 'f' AND confrelid = 'auth_user'::regclass
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', fk.conrelid::regclass, fk.conname);
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT %I %s',
                fk.conrelid::regclass,
                fk.conname,
                replace(fk.definition, 'REFERENCES auth_user', 'REFERENCES platform_users')
            );
        END LOOP;
        PERFORM setval(pg_get_serial_sequence('platform_users', 'id'),
                       GREATEST(COALESCE((SELECT MAX(id) FROM platform_users), 1), 1), true);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS platform_session_activity (
    token_hash CHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS platform_session_activity_user_idx
    ON platform_session_activity (user_id, last_activity);
