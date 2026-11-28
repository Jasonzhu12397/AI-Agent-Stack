use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::plugin_http::invoke_operation;
use ai_agent_stack_api::proxy::AppState;
use axum::extract::Json as Body;
use axum::http::Uri;
use serde_json::{json, Value};

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-http-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugincredential (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, sealed_value TEXT NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    AppState::new(config).unwrap()
}

#[tokio::test]
async fn renders_an_operation_from_the_manifest() {
    let echo = axum::Router::new().route(
        "/echo/{id}",
        axum::routing::post(|uri: Uri, Body(body): Body<Value>| async move {
            axum::Json(json!({
                "path": uri.path(),
                "query": uri.query().unwrap_or(""),
                "body": body,
            }))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, echo).await.unwrap();
    });

    let state = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    let manifest = json!({
        "http": {
            "base_url": format!("http://{address}"),
            "operations": {
                "echo": {
                    "method": "POST",
                    "path": "/echo/{id}",
                    "path_params": {"id": "id"},
                    "query_params": {"q": "query"},
                    "body_param": "payload",
                }
            }
        }
    });
    let result = invoke_operation(
        ai_agent_stack_api::plugin_http::ConnectorAuth {
            state: &state,
            pool,
            tenant_id: TENANT,
            credential_ref: "",
            auth_required: false,
        },
        &manifest,
        "echo",
        &json!({"id": "a/b", "query": "西安", "payload": {"hello": "world"}}),
        30,
    )
    .await
    .unwrap();
    assert_eq!(result["path"], json!("/echo/a%2Fb"));
    assert_eq!(result["query"], json!("q=%E8%A5%BF%E5%AE%89"));
    assert_eq!(result["body"], json!({"hello": "world"}));
}
