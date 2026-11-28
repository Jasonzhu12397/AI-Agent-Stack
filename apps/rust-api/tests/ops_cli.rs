use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::AppState;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const NODE: &str = "33333333-3333-3333-3333-333333333333";
const DEPLOYMENT: &str = "44444444-4444-4444-4444-444444444444";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-ops-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",
        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, eph_node_id TEXT NOT NULL, target_node_id TEXT, requested_by_id INTEGER NOT NULL, cluster_name VARCHAR(120) NOT NULL, kubernetes_version VARCHAR(40) NOT NULL, spec TEXT NOT NULL, plan_result TEXT NOT NULL, preflight_result TEXT NOT NULL, status VARCHAR(32) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, error TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentcommand (id TEXT PRIMARY KEY, command_type VARCHAR(80) NOT NULL, payload TEXT NOT NULL, status VARCHAR(16) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, plan_hash VARCHAR(64) NOT NULL, attempts INTEGER NOT NULL, result TEXT, error TEXT, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, node_id TEXT, project_id TEXT NOT NULL, requested_by_id INTEGER, tenant_id TEXT NOT NULL, deployment_id TEXT)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT)
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO core_clusterdeployment (id, tenant_id, project_id, eph_node_id, target_node_id, \
         requested_by_id, cluster_name, kubernetes_version, spec, plan_result, preflight_result, \
         status, idempotency_key, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, \
         'lab-cluster', 'v1.31.0', '{\"kubernetes\":{}}', '{}', '{}', 'succeeded', 'key', '{}', \
         datetime('now'), datetime('now'))",
    )
    .bind(DEPLOYMENT)
    .bind(TENANT)
    .bind(PROJECT)
    .bind(NODE)
    .bind(NODE)
    .execute(&pool)
    .await
    .unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    AppState::new(config).unwrap()
}

async fn audit_count(state: &AppState, event: &str) -> i64 {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM core_auditevent WHERE event_type = ?")
        .bind(event)
        .fetch_one(state.identity_pool.as_ref().unwrap())
        .await
        .unwrap()
}

#[tokio::test]
async fn check_backups_reports_once_then_closes_with_a_recovery_event() {
    let state = state().await;
    let code = ai_agent_stack_api::ops_cli::run(&state, "check-backups", &[])
        .await
        .unwrap();
    assert_eq!(code, 1);
    assert_eq!(audit_count(&state, "cluster.etcd_backup.problem").await, 1);
    // A persisting problem is written once, not once per probe.
    let code = ai_agent_stack_api::ops_cli::run(&state, "check-backups", &[])
        .await
        .unwrap();
    assert_eq!(code, 1);
    assert_eq!(audit_count(&state, "cluster.etcd_backup.problem").await, 1);
    // The scheduler heartbeat is silent, so the finding names that cause.
    let metadata: String = sqlx::query_scalar(
        "SELECT metadata FROM core_auditevent WHERE event_type = 'cluster.etcd_backup.problem'",
    )
    .fetch_one(state.identity_pool.as_ref().unwrap())
    .await
    .unwrap();
    assert!(
        metadata.contains("ETCD_BACKUP_SCHEDULER_SILENT"),
        "{metadata}"
    );

    let pool = state.identity_pool.as_ref().unwrap();
    sqlx::query(
        "INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, \
         plan_hash, attempts, result, created_at, updated_at, finished_at, node_id, project_id, \
         tenant_id, deployment_id) VALUES ('cmd-backup', 'cluster.kubernetes.backup.run', '{}', \
         'succeeded', 'k', 'p', 1, \
         '{\"backup\":{\"name\":\"lab-cluster-20261128T140000Z.db\"}}', datetime('now'), \
         datetime('now'), datetime('now'), ?, ?, ?, ?)",
    )
    .bind(NODE)
    .bind(PROJECT)
    .bind(TENANT)
    .bind(DEPLOYMENT)
    .execute(pool)
    .await
    .unwrap();
    let code = ai_agent_stack_api::ops_cli::run(&state, "check-backups", &[])
        .await
        .unwrap();
    assert_eq!(code, 0);
    assert_eq!(audit_count(&state, "cluster.etcd_backup.problem").await, 1);
    assert_eq!(
        audit_count(&state, "cluster.etcd_backup.recovered").await,
        1
    );
}

#[tokio::test]
async fn check_backups_dry_run_never_writes() {
    let state = state().await;
    let code = ai_agent_stack_api::ops_cli::run(&state, "check-backups", &["--dry-run".into()])
        .await
        .unwrap();
    assert_eq!(code, 1);
    assert_eq!(audit_count(&state, "cluster.etcd_backup.problem").await, 0);
}
