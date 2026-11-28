-- Copy legacy Django accounts exactly once; fresh databases have no auth_user.
INSERT OR IGNORE INTO platform_users (id, username, email, first_name, last_name, is_active, is_staff, is_superuser, last_login, date_joined)
SELECT CAST(id AS INTEGER), username, COALESCE(email, ''), COALESCE(first_name, ''), COALESCE(last_name, ''),
       is_active, is_staff, is_superuser, last_login, date_joined
FROM auth_user;

INSERT OR IGNORE INTO platform_credentials (user_id, credential_type, credential_hash)
SELECT CAST(id AS INTEGER), 'password', password FROM auth_user;
