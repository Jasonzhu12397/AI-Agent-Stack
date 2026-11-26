use std::net::SocketAddr;

use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::TryStreamExt;
use serde_json::json;

use crate::config::Config;

const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

#[derive(Clone)]
pub struct AppState {
    pub config: Config,
    pub client: reqwest::Client,
    pub identity_pool: Option<sqlx::AnyPool>,
}

impl AppState {
    pub fn new(config: Config) -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(config.request_timeout)
            .build()
            .map_err(|error| format!("failed to build the upstream HTTP client: {error}"))?;
        let identity_pool = match &config.database_url {
            Some(url) => {
                sqlx::any::install_default_drivers();
                Some(
                    sqlx::AnyPool::connect_lazy(url)
                        .map_err(|error| format!("DATABASE_URL is invalid: {error}"))?,
                )
            }
            None => None,
        };
        Ok(Self {
            config,
            client,
            identity_pool,
        })
    }
}

pub fn router(state: AppState) -> Router {
    let native_identity = state.config.native_identity;
    let mut router = Router::new()
        .route("/health/ready", get(health))
        .route("/api/v1/edge/info", get(info))
        .route("/ws/{*path}", get(crate::ws::forward));
    if native_identity {
        router = router
            .route("/api/v1/auth/csrf/", get(crate::auth::csrf))
            .route("/api/v1/auth/login/", post(crate::auth::login))
            .route("/api/v1/auth/logout/", post(crate::auth::logout))
            .route("/api/v1/auth/me/", get(crate::native::current_user));
    }
    router.fallback(forward).with_state(state)
}

async fn health(State(state): State<AppState>) -> Response {
    let started = std::time::Instant::now();
    match state
        .client
        .get(state.config.upstream_health_url())
        .timeout(state.config.upstream_timeout)
        .send()
        .await
    {
        Ok(response) if response.status().is_success() => Json(json!({
            "status": "ready",
            "service": "rust-api-edge",
            "upstream_status": response.status().as_u16(),
            "latency_ms": started.elapsed().as_millis() as u64,
        }))
        .into_response(),
        Ok(response) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({
                "status": "degraded",
                "service": "rust-api-edge",
                "upstream_status": response.status().as_u16(),
            })),
        )
            .into_response(),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "status": "unavailable",
                "service": "rust-api-edge",
                "error": error.to_string(),
            })),
        )
            .into_response(),
    }
}

#[derive(serde::Serialize)]
struct EdgeInfo {
    service: &'static str,
    version: &'static str,
    upstream: String,
    websocket: bool,
    server_sent_events: bool,
}

async fn info(State(state): State<AppState>) -> Json<EdgeInfo> {
    Json(EdgeInfo {
        service: "rust-api-edge",
        version: env!("CARGO_PKG_VERSION"),
        upstream: state.config.upstream.to_string(),
        websocket: true,
        server_sent_events: true,
    })
}

async fn forward(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request<Body>,
) -> Response {
    let method = request.method().clone();
    let uri = request.uri().clone();
    let incoming_headers = request.headers().clone();
    let body = request.into_body();

    let target = format!(
        "{}{}",
        state.config.upstream.as_str().trim_end_matches('/'),
        uri.path_and_query()
            .map(|value| value.as_str())
            .unwrap_or("/")
    );

    let mut builder = state.client.request(
        reqwest::Method::from_bytes(method.as_str().as_bytes()).unwrap_or(reqwest::Method::GET),
        &target,
    );

    let mut outbound = HeaderMap::new();
    for (name, value) in incoming_headers.iter() {
        if HOP_BY_HOP.contains(&name.as_str()) {
            continue;
        }
        outbound.append(name, value.clone());
    }
    if !outbound.contains_key("x-request-id") {
        outbound.insert(
            HeaderName::from_static("x-request-id"),
            HeaderValue::from_str(&uuid::Uuid::new_v4().to_string())
                .unwrap_or_else(|_| HeaderValue::from_static("missing-request-id")),
        );
    }
    let forwarded_for = match outbound.get("x-forwarded-for") {
        Some(existing) => format!("{}, {}", existing.to_str().unwrap_or_default(), peer.ip()),
        None => peer.ip().to_string(),
    };
    if !forwarded_for.is_empty() {
        if let Ok(value) = HeaderValue::from_str(&forwarded_for) {
            outbound.insert(HeaderName::from_static("x-forwarded-for"), value);
        }
    }
    if !outbound.contains_key("x-forwarded-proto") {
        outbound.insert(
            HeaderName::from_static("x-forwarded-proto"),
            HeaderValue::from_static("http"),
        );
    }
    builder = builder.headers(outbound);

    let content_length = incoming_headers
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok());
    if content_length.unwrap_or(0) > 0 || method != axum::http::Method::GET {
        builder = builder.body(reqwest::Body::wrap_stream(body.into_data_stream()));
    }

    let upstream_response = match builder.send().await {
        Ok(response) => response,
        Err(error) => {
            tracing::warn!(%target, %error, "rust api edge could not reach the upstream");
            return (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": {
                        "code": "UPSTREAM_UNAVAILABLE",
                        "message": "The platform API could not be reached through the Rust edge.",
                    }
                })),
            )
                .into_response();
        }
    };

    let status = upstream_response.status();
    let headers = upstream_response.headers().clone();
    let mut response = Response::new(Body::from_stream(upstream_response.bytes_stream().map_err(
        |error| std::io::Error::other(format!("upstream response stream failed: {error}")),
    )));
    *response.status_mut() = status;
    for (name, value) in headers.iter() {
        if HOP_BY_HOP.contains(&name.as_str()) {
            continue;
        }
        response.headers_mut().append(name, value.clone());
    }
    response
}
