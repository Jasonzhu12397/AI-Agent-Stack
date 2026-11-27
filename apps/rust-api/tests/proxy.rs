use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-proxy-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    AppState::new(Config::for_test(
        "127.0.0.1:0".into(),
        url,
        Some("test-secret".into()),
    ))
    .unwrap()
}

#[tokio::test]
async fn native_edge_info_is_served_without_an_upstream() {
    let app = router(state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/edge/info")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: serde_json::Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
    assert_eq!(body["service"], "rust-api");
}

#[tokio::test]
async fn readiness_reports_native_identity() {
    let app = router(state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health/ready")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: serde_json::Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
    assert_eq!(body["status"], "ready");
}
