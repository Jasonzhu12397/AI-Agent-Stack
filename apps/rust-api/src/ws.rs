use std::net::SocketAddr;
use std::time::Duration;

use axum::body::Body;
use axum::extract::ws::{CloseCode, CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::connect_async_with_config;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode as UpstreamCloseCode;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::protocol::CloseFrame as UpstreamCloseFrame;
use tokio_tungstenite::tungstenite::Message as UpstreamMessage;

use crate::proxy::AppState;

const FORWARDED_HEADERS: &[&str] = &[
    "origin",
    "cookie",
    "authorization",
    "user-agent",
    "accept-language",
    "sec-websocket-protocol",
    "x-request-id",
    "x-session-token",
    "x-forwarded-proto",
];

/// Terminate the browser handshake, open the same logical connection upstream,
/// and relay frames in both directions.
///
/// Django Channels authenticates the upgrade from either the session cookie or
/// the `session.<key>` subprotocol. Both travel in headers, so the upstream
/// handshake must carry the original `Cookie`, `Origin` and
/// `Sec-WebSocket-Protocol` values; the body is irrelevant after the upgrade.
pub async fn forward(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    ws: WebSocketUpgrade,
    request: Request<Body>,
) -> Response {
    let permit = match state.websocket_slots.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            return (StatusCode::SERVICE_UNAVAILABLE, axum::Json(serde_json::json!({
                "error": {"code": "WEBSOCKET_CAPACITY_EXCEEDED", "message": "WebSocket capacity is temporarily exhausted."}
            }))).into_response();
        }
    };
    let path_and_query = request
        .uri()
        .path_and_query()
        .map(|value| value.as_str().to_string());
    let headers = request.headers().clone();
    let target = match state
        .config
        .upstream_websocket_url(path_and_query.as_deref())
    {
        Ok(url) => url,
        Err(error) => {
            tracing::warn!(%error, "rust api edge could not derive the websocket upstream");
            return (
                StatusCode::BAD_GATEWAY,
                axum::Json(serde_json::json!({
                    "error": {
                        "code": "WEBSOCKET_UPSTREAM_INVALID",
                        "message": "The websocket upstream could not be derived.",
                    }
                })),
            )
                .into_response();
        }
    };

    // Echo the subset the browser offered. Axum picks the first match, which is
    // `aiagentstack` for the console; the session token stays in the upstream
    // handshake where Django reads it.
    let offered: Vec<String> = headers
        .get("sec-websocket-protocol")
        .and_then(|value| value.to_str().ok())
        .map(|value| {
            value
                .split(',')
                .map(|item| item.trim().to_string())
                .filter(|item| !item.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let ws = ws.max_message_size(1_048_576).max_frame_size(1_048_576);
    let ws = if offered.is_empty() {
        ws
    } else {
        ws.protocols(offered)
    };

    ws.on_upgrade(move |client| async move {
        let _permit = permit;
        relay(client, target, headers, peer).await;
    })
    .into_response()
}

async fn relay(client: WebSocket, target: reqwest::Url, headers: HeaderMap, peer: SocketAddr) {
    let upstream_request = match build_upstream_request(&target, &headers, peer) {
        Ok(request) => request,
        Err(error) => {
            tracing::warn!(%target, %error, "rust api edge could not build the websocket handshake");
            close_client(client, 1011, "invalid websocket upstream request").await;
            return;
        }
    };

    let websocket_config = WebSocketConfig {
        max_message_size: Some(1_048_576),
        max_frame_size: Some(1_048_576),
        ..WebSocketConfig::default()
    };
    let upstream_result = tokio::time::timeout(
        Duration::from_secs(10),
        connect_async_with_config(upstream_request, Some(websocket_config), false),
    ).await;
    let upstream = match upstream_result {
        Ok(Ok((socket, response))) => {
            tracing::info!(%target, status = response.status().as_u16(), "rust api edge upgraded the websocket upstream");
            socket
        }
        Ok(Err(error)) => {
            tracing::warn!(%target, %error, "rust api edge could not reach the websocket upstream");
            close_client(client, 1011, "websocket upstream unavailable").await;
            return;
        }
        Err(_) => {
            tracing::warn!(%target, "rust api edge websocket upstream handshake timed out");
            close_client(client, 1011, "websocket upstream timed out").await;
            return;
        }
    };

    let (mut client_tx, mut client_rx) = client.split();
    let (mut upstream_tx, mut upstream_rx) = upstream.split();

    let result = {
        tokio::select! {
            result = client_to_upstream(&mut client_rx, &mut upstream_tx) => result,
            result = upstream_to_client(&mut upstream_rx, &mut client_tx) => result,
        }
    };

    if let Err(error) = result {
        tracing::debug!(%target, %error, "websocket relay finished with an error");
        let _ = client_tx
            .send(Message::Close(Some(CloseFrame {
                code: CloseCode::from(1011u16),
                reason: "websocket relay failed".into(),
            })))
            .await;
        let _ = upstream_tx
            .send(UpstreamMessage::Close(Some(UpstreamCloseFrame {
                code: UpstreamCloseCode::Error,
                reason: "websocket relay failed".into(),
            })))
            .await;
    }
}

async fn client_to_upstream(
    client_rx: &mut futures_util::stream::SplitStream<WebSocket>,
    upstream_tx: &mut futures_util::stream::SplitSink<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        UpstreamMessage,
    >,
) -> Result<(), String> {
    while let Some(message) = client_rx.next().await {
        let message = message.map_err(|error| error.to_string())?;
        match message {
            Message::Text(text) => upstream_tx
                .send(UpstreamMessage::Text(text.as_str().to_owned().into()))
                .await
                .map_err(|error| error.to_string())?,
            Message::Binary(bytes) => upstream_tx
                .send(UpstreamMessage::Binary(bytes))
                .await
                .map_err(|error| error.to_string())?,
            Message::Ping(bytes) => upstream_tx
                .send(UpstreamMessage::Ping(bytes))
                .await
                .map_err(|error| error.to_string())?,
            Message::Pong(bytes) => upstream_tx
                .send(UpstreamMessage::Pong(bytes))
                .await
                .map_err(|error| error.to_string())?,
            Message::Close(frame) => {
                let _ = upstream_tx
                    .send(UpstreamMessage::Close(frame.map(close_to_upstream)))
                    .await;
                return Ok(());
            }
        }
    }
    let _ = upstream_tx.send(UpstreamMessage::Close(None)).await;
    Ok(())
}

async fn upstream_to_client(
    upstream_rx: &mut futures_util::stream::SplitStream<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
    >,
    client_tx: &mut futures_util::stream::SplitSink<WebSocket, Message>,
) -> Result<(), String> {
    while let Some(message) = upstream_rx.next().await {
        let message = message.map_err(|error| error.to_string())?;
        match message {
            UpstreamMessage::Text(text) => client_tx
                .send(Message::Text(text.as_str().to_owned().into()))
                .await
                .map_err(|error| error.to_string())?,
            UpstreamMessage::Binary(bytes) => client_tx
                .send(Message::Binary(bytes))
                .await
                .map_err(|error| error.to_string())?,
            UpstreamMessage::Ping(bytes) => client_tx
                .send(Message::Ping(bytes))
                .await
                .map_err(|error| error.to_string())?,
            UpstreamMessage::Pong(bytes) => client_tx
                .send(Message::Pong(bytes))
                .await
                .map_err(|error| error.to_string())?,
            UpstreamMessage::Close(frame) => {
                let _ = client_tx
                    .send(Message::Close(frame.map(close_to_client)))
                    .await;
                return Ok(());
            }
            UpstreamMessage::Frame(_) => {}
        }
    }
    let _ = client_tx.send(Message::Close(None)).await;
    Ok(())
}

fn build_upstream_request(
    target: &reqwest::Url,
    headers: &HeaderMap,
    peer: SocketAddr,
) -> Result<Request<()>, String> {
    let mut builder = Request::builder()
        .method("GET")
        .uri(target.as_str())
        .header("connection", "Upgrade")
        .header("upgrade", "websocket")
        .header("sec-websocket-version", "13")
        .header(
            "sec-websocket-key",
            base64::engine::general_purpose::STANDARD.encode(uuid::Uuid::new_v4().as_bytes()),
        );
    for name in FORWARDED_HEADERS {
        let Some(value) = headers.get(*name) else {
            continue;
        };
        builder = builder.header(*name, value.clone());
    }
    if let Some(host) = headers.get("host") {
        builder = builder.header("host", host.clone());
    }
    let forwarded_for = match headers.get("x-forwarded-for") {
        Some(existing) => format!("{}, {}", existing.to_str().unwrap_or_default(), peer.ip()),
        None => peer.ip().to_string(),
    };
    if let Ok(value) = HeaderValue::from_str(&forwarded_for) {
        builder = builder.header(HeaderName::from_static("x-forwarded-for"), value);
    }
    builder
        .body(())
        .map_err(|error| format!("invalid upstream websocket request: {error}"))
}

fn close_to_upstream(frame: CloseFrame) -> UpstreamCloseFrame {
    UpstreamCloseFrame {
        code: UpstreamCloseCode::from(frame.code),
        reason: frame.reason.to_string().into(),
    }
}

fn close_to_client(frame: UpstreamCloseFrame) -> CloseFrame {
    CloseFrame {
        code: CloseCode::from(u16::from(frame.code)),
        reason: frame.reason.to_string().into(),
    }
}

async fn close_client(client: WebSocket, code: u16, reason: &str) {
    let mut client = client;
    let _ = client
        .send(Message::Close(Some(CloseFrame {
            code: CloseCode::from(code),
            reason: reason.to_string().into(),
        })))
        .await;
}
