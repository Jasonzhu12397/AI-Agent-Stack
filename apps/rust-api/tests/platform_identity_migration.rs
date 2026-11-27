use sqlx::PgPool;

#[tokio::test]
async fn migrates_legacy_accounts_into_rust_owned_identity_tables() {
    let database_url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must point to the disposable PostgreSQL CI database");
    let pool = PgPool::connect(&database_url)
        .await
        .expect("connect to PostgreSQL");

    sqlx::raw_sql(
        "DROP TABLE IF EXISTS auth_user CASCADE;
         DROP TABLE IF EXISTS platform_user_profiles CASCADE;
         DROP TABLE IF EXISTS platform_session_activity CASCADE;
         DROP TABLE IF EXISTS rust_sessions CASCADE;
         DROP TABLE IF EXISTS platform_credentials CASCADE;
         DROP TABLE IF EXISTS platform_users CASCADE;",
    )
    .execute(&pool)
    .await
    .expect("reset isolated migration test schema");

    sqlx::raw_sql(include_str!("../migrations/0001_rust_sessions.sql"))
        .execute(&pool)
        .await
        .expect("create Rust identity tables");

    sqlx::raw_sql(
        "CREATE TABLE auth_user (
            id BIGINT PRIMARY KEY,
            username VARCHAR(150) NOT NULL UNIQUE,
            email VARCHAR(254) NOT NULL DEFAULT '',
            first_name VARCHAR(150) NOT NULL DEFAULT '',
            last_name VARCHAR(150) NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_staff BOOLEAN NOT NULL DEFAULT FALSE,
            is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
            last_login TIMESTAMPTZ NULL,
            date_joined TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            password TEXT NOT NULL
        );
        INSERT INTO auth_user
            (id, username, email, first_name, last_name, is_active, is_staff,
             is_superuser, date_joined, password)
        VALUES
            (101, 'migration-user', 'migration@example.test', 'Migration', 'User',
             TRUE, FALSE, FALSE, CURRENT_TIMESTAMP, 'pbkdf2_sha256$test-hash');",
    )
    .execute(&pool)
    .await
    .expect("prepare legacy account fixture");

    sqlx::raw_sql(include_str!("../migrations/0002_platform_identity.sql"))
        .execute(&pool)
        .await
        .expect("transfer legacy identity data to Rust tables");

    sqlx::raw_sql(include_str!("../migrations/0003_platform_user_profiles.sql"))
        .execute(&pool)
        .await
        .expect("create Rust-owned user profiles");

    let account: (i64, String, String) = sqlx::query_as(
        "SELECT users.id, users.username, credentials.credential_hash
         FROM platform_users users
         JOIN platform_credentials credentials ON credentials.user_id = users.id
         WHERE users.username = 'migration-user'
           AND credentials.credential_type = 'password'",
    )
    .fetch_one(&pool)
    .await
    .expect("read migrated account and password hash");

    assert_eq!(account.0, 101);
    assert_eq!(account.1, "migration-user");
    assert_eq!(account.2, "pbkdf2_sha256$test-hash");

    let profile_table_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'platform_user_profiles')",
    )
    .fetch_one(&pool)
    .await
    .expect("verify Rust profile table exists");
    assert!(profile_table_exists);

    let next_id: i64 = sqlx::query_scalar(
        "INSERT INTO platform_users (username) VALUES ('sequence-check') RETURNING id",
    )
    .fetch_one(&pool)
    .await
    .expect("insert account using the synchronized identity sequence");
    assert!(
        next_id > 101,
        "identity sequence must advance past imported IDs"
    );
}
