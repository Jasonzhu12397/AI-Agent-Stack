use std::net::SocketAddr;
use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::{to_bytes, Body};
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use axum::response::IntoResponse;
use axum::routing::any;
use axum::{Json, Router};
use serde_json::json;
use tower::ServiceExt;

async fn start_upstream() -> String {
    let upstream = Router::new().fallback(any(|request: Request<Body>| async move {
        let method = request.method().to_string();
        let uri = request.uri().to_string();
        let request_id = request
            .headers()
            .get("x-request-id")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("missing")
            .to_string();
        let host = request
            .headers()
            .get("host")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("missing")
            .to_string();
        let forwarded_for = request
            .headers()
            .get("x-forwarded-for")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("missing")
            .to_string();
        let body = to_bytes(request.into_body(), 1024 * 1024)
            .await
            .map(|bytes| bytes.to_vec())
            .unwrap_or_default();
        let mut response = Json(json!({
            "method": method,
            "uri": uri,
            "request_id": request_id,
            "host": host,
            "forwarded_for": forwarded_for,
            "body": String::from_utf8_lossy(&body).to_string(),
        }))
        .into_response();
        response
            .headers_mut()
            .insert("x-upstream", "mock".parse().unwrap());
        response.headers_mut().insert(
            "set-cookie",
            "sessionid=abc123; Path=/; HttpOnly".parse().unwrap(),
        );
        response
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, upstream).await.unwrap();
    });
    format!("http://{address}")
}

fn state(upstream: String) -> AppState {
    AppState::new(
        Config::new(
            "127.0.0.1:0".into(),
            upstream,
            Duration::from_secs(2),
            Duration::from_secs(10),
        )
        .unwrap(),
    )
    .unwrap()
}

fn request(method: &str, uri: &str, body: Body) -> Request<Body> {
    let mut builder = Request::builder().method(method).uri(uri);
    if method == "POST" {
        builder = builder.header("content-type", "application/json");
    }
    builder
        .extension(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 12345))))
        .body(body)
        .unwrap()
}

#[tokio::test]
async fn forwards_method_path_query_headers_and_body() {
    let upstream = start_upstream().await;
    let app = router(state(upstream));

    let mut inbound = request(
        "POST",
        "/api/v1/tenants/abc/tasks/?limit=2",
        Body::from(r#"{"hello":"world"}"#),
    );
    inbound
        .headers_mut()
        .insert("x-request-id", "req-123".parse().unwrap());
    inbound
        .headers_mut()
        .insert("host", "console.example.test".parse().unwrap());
    let response = app.oneshot(inbound).await.unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers().get("x-upstream").unwrap(), "mock");
    assert_eq!(
        response.headers().get("set-cookie").unwrap(),
        "sessionid=abc123; Path=/; HttpOnly"
    );
    let body: serde_json::Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
    assert_eq!(body["method"], "POST");
    assert_eq!(body["uri"], "/api/v1/tenants/abc/tasks/?limit=2");
    assert_eq!(body["request_id"], "req-123");
    assert_eq!(body["host"], "console.example.test");
    assert_eq!(body["forwarded_for"], "127.0.0.1");
    assert_eq!(body["body"], r#"{"hello":"world"}"#);
}

#[tokio::test]
async fn adds_a_request_id_for_calls_without_one() {
    let upstream = start_upstream().await;
    let app = router(state(upstream));

    let proxied = app
        .oneshot(request("GET", "/api/v1/anything", Body::empty()))
        .await
        .unwrap();
    let body: serde_json::Value =
        serde_json::from_slice(&to_bytes(proxied.into_body(), 4096).await.unwrap()).unwrap();
    assert_ne!(body["request_id"], "missing");
}

#[tokio::test]
async fn native_edge_info_is_served_without_the_upstream() {
    let app = router(state("http://127.0.0.1:9".into()));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/edge/info")
                .extension(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 12345))))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn health_fails_when_the_upstream_is_down() {
    let app = router(state("http://127.0.0.1:9".into()));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health/ready")
                .extension(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 12345))))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn rejects_session_cookie_mutations_without_csrf_tokens() {
    let app = router(state("http://127.0.0.1:9".into()));
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/legacy/mutation/")
                .header("cookie", "sessionid=browser-session")
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}
