use ai_agent_stack_api::token_guard::{release, reserve_adhoc, settle, AdHocRequest};

async fn pool() -> sqlx::AnyPool {
    sqlx::any::install_default_drivers();
    let path = std::env::temp_dir().join(format!(
        "rust-adhoc-budget-{}.sqlite3",
        uuid::Uuid::new_v4()
    ));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE core_tenanttokenbudget (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL UNIQUE, daily_limit INTEGER NOT NULL, monthly_limit INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenusage (id TEXT PRIMARY KEY, idempotency_key VARCHAR(160) NOT NULL, status VARCHAR(16) NOT NULL, reserved_tokens INTEGER NOT NULL, consumed_tokens INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, agent_run_id TEXT, task_id TEXT, tenant_id TEXT NOT NULL, workflow_run_id TEXT, settled_at DATETIME, scope_ref VARCHAR(160), UNIQUE(tenant_id, idempotency_key))",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_tenanttokenbudget (tenant_id, daily_limit, monthly_limit, created_at, updated_at) VALUES ('tenant', 150, 1000, datetime('now'), datetime('now'))")
        .execute(&pool).await.unwrap();
    pool
}

#[tokio::test]
async fn reserves_settles_and_replays_adhoc_budget_rows() {
    let pool = pool().await;
    let reservation = reserve_adhoc(
        &pool,
        false,
        AdHocRequest {
            tenant_id: "tenant",
            scope_ref: "agent-chat:thread-1",
            idempotency_key: "agent-chat:thread-1:message-1",
            requested_tokens: 100,
        },
    )
    .await
    .unwrap();
    assert_eq!(reservation.reserved_tokens, 100);

    let replay = reserve_adhoc(
        &pool,
        false,
        AdHocRequest {
            tenant_id: "tenant",
            scope_ref: "agent-chat:thread-1",
            idempotency_key: "agent-chat:thread-1:message-1",
            requested_tokens: 100,
        },
    )
    .await
    .unwrap();
    assert_eq!(replay.id, reservation.id);

    settle(&pool, &reservation, 80).await.unwrap();
    let status: String =
        sqlx::query_scalar("SELECT status FROM core_tenanttokenusage WHERE CAST(id AS TEXT) = ?")
            .bind(&reservation.id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(status, "settled");

    let second = reserve_adhoc(
        &pool,
        false,
        AdHocRequest {
            tenant_id: "tenant",
            scope_ref: "rag-ingest:doc-1",
            idempotency_key: "rag-ingest:doc-1:0",
            requested_tokens: 60,
        },
    )
    .await
    .unwrap();
    release(&pool, &second).await.unwrap();

    let error = reserve_adhoc(
        &pool,
        false,
        AdHocRequest {
            tenant_id: "tenant",
            scope_ref: "agent-chat:thread-1",
            idempotency_key: "agent-chat:thread-1:message-2",
            requested_tokens: 90,
        },
    )
    .await
    .unwrap_err();
    assert!(error.starts_with("TENANT_TOKEN_BUDGET_EXCEEDED"), "{error}");

    let wrong_scope = reserve_adhoc(
        &pool,
        false,
        AdHocRequest {
            tenant_id: "tenant",
            scope_ref: "another-scope",
            idempotency_key: "agent-chat:thread-1:message-1",
            requested_tokens: 100,
        },
    )
    .await
    .unwrap_err();
    assert_eq!(
        wrong_scope,
        "Token reservation idempotency key belongs to another scope."
    );
}
