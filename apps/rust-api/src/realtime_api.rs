#![allow(clippy::result_large_err)]

use std::time::Duration;

use axum::body::Body;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{DateTime, SecondsFormat, Utc};
use futures_util::stream::{self, Stream};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access, tenant_role};

/// PostgreSQL notification channel the write triggers publish on.
const CHANNEL: &str = "platform_events";

#[derive(Deserialize, Default)]
pub struct StreamQuery {
    since: Option<String>,
    max_events: Option<String>,
}

struct StreamState {
    pool: AnyPool,
    postgres: bool,
    tenant_id: String,
    project_id: String,
    audit_visible: bool,
    cursor: String,
    max_events: Option<i64>,
    emitted: i64,
    ticks: i64,
    listener: Option<sqlx::postgres::PgListener>,
}

impl StreamState {
    fn at_cursor(&self, cursor: &str) -> StreamState {
        StreamState {
            pool: self.pool.clone(),
            postgres: self.postgres,
            tenant_id: self.tenant_id.clone(),
            project_id: self.project_id.clone(),
            audit_visible: self.audit_visible,
            cursor: cursor.to_string(),
            max_events: self.max_events,
            emitted: 0,
            ticks: 0,
            listener: None,
        }
    }
}

/// Wait for one notification that belongs to this project. PostgreSQL is the
/// wakeup source; the query after every wakeup stays the source of truth, so a
/// dropped notification can only delay an event, never lose it.
async fn await_scope_notification(
    listener: &mut sqlx::postgres::PgListener,
    tenant_id: &str,
    project_id: &str,
) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return;
        }
        match tokio::time::timeout(remaining, listener.recv()).await {
            Ok(Ok(notification)) => {
                let payload: Value =
                    serde_json::from_str(notification.payload()).unwrap_or(Value::Null);
                let tenant = payload.get("tenant_id").and_then(Value::as_str);
                let project = payload.get("project_id").and_then(Value::as_str);
                if tenant == Some(tenant_id) && project == Some(project_id) {
                    return;
                }
            }
            Ok(Err(error)) => {
                tracing::warn!(%error, "realtime listener stopped");
                return;
            }
            Err(_) => return,
        }
    }
}

fn stream_error(detail: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"detail": detail}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "Database is not configured."})),
    )
        .into_response()
}

fn database_error(error: impl std::fmt::Display) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"detail": format!("Database error: {error}")})),
    )
        .into_response()
}

fn parse_cursor(value: Option<&str>) -> String {
    match value {
        Some(value) if !value.is_empty() => DateTime::parse_from_rfc3339(value)
            .map(|time| {
                time.with_timezone(&Utc)
                    .to_rfc3339_opts(SecondsFormat::Micros, false)
            })
            .unwrap_or_else(|_| Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false)),
        _ => Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false),
    }
}

fn timestamp_column(column: &str, postgres: bool) -> String {
    if postgres {
        format!("to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') AS {column}")
    } else {
        // SQLite's %f is "seconds with fraction" (\`SS.SSS\`), not a bare
        // fraction: appending ".%f" to %S would print the seconds twice.
        format!("strftime('%Y-%m-%d %H:%M:%f', {column}) AS {column}")
    }
}

async fn collect_events(
    state: &StreamState,
) -> Result<(Vec<(String, String, Value)>, String), String> {
    let ts = timestamp_column("updated_at", state.postgres);
    let tasks = sqlx::query(&format!(
        "SELECT CAST(id AS TEXT) AS id, status, task_type, attempts, {ts} \
         FROM core_task WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         AND updated_at > ? ORDER BY updated_at, CAST(id AS TEXT) LIMIT 50"
    ))
    .bind(&state.tenant_id)
    .bind(&state.project_id)
    .bind(&state.cursor)
    .fetch_all(&state.pool)
    .await
    .map_err(|error| error.to_string())?;
    let mut items = Vec::new();
    for row in tasks {
        let stamp: String = row.try_get("updated_at").unwrap_or_default();
        let stamp = normalize_timestamp(&stamp);
        let event = json!({
            "type": "task",
            "id": row.try_get::<String, _>("id").unwrap_or_default(),
            "status": row.try_get::<String, _>("status").unwrap_or_default(),
            "task_type": row.try_get::<String, _>("task_type").unwrap_or_default(),
            "attempts": row.try_get::<i64, _>("attempts").unwrap_or(0),
            "updated_at": stamp.clone(),
        });
        items.push((stamp.clone(), stamp, event));
    }
    if state.audit_visible {
        let created = timestamp_column("created_at", state.postgres);
        let rows = sqlx::query(&format!(
            "SELECT CAST(id AS TEXT) AS id, event_type, resource_type, resource_id, outcome, {created} \
             FROM core_auditevent WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
             AND created_at > ? ORDER BY created_at, CAST(id AS TEXT) LIMIT 50"
        ))
        .bind(&state.tenant_id)
        .bind(&state.project_id)
        .bind(&state.cursor)
        .fetch_all(&state.pool)
        .await
        .map_err(|error| error.to_string())?;
        for row in rows {
            let stamp: String = row.try_get("created_at").unwrap_or_default();
            let stamp = normalize_timestamp(&stamp);
            let event = json!({
                "type": "audit",
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "event_type": row.try_get::<String, _>("event_type").unwrap_or_default(),
                "resource_type": row.try_get::<String, _>("resource_type").unwrap_or_default(),
                "resource_id": row.try_get::<String, _>("resource_id").unwrap_or_default(),
                "outcome": row.try_get::<String, _>("outcome").unwrap_or_default(),
                "created_at": stamp.clone(),
            });
            items.push((stamp.clone(), stamp, event));
        }
    }
    items.sort_by(|left, right| left.0.cmp(&right.0).then(left.1.cmp(&right.1)));
    items.truncate(50);
    let next = items
        .last()
        .map(|item| item.0.clone())
        .unwrap_or_else(|| state.cursor.clone());
    Ok((items, next))
}

fn frame(event: &Value, event_id: &str) -> String {
    format!(
        "id: {event_id}\nevent: {}\ndata: {}\n\n",
        event
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("message"),
        event
    )
}

fn stream_body(state: StreamState) -> impl Stream<Item = Result<String, std::io::Error>> {
    stream::unfold(Some(state), move |state| async move {
        let mut state = state?;
        if state.max_events.is_some() && state.emitted >= state.max_events.unwrap_or(0) {
            return None;
        }
        if state.max_events.is_none() && state.ticks > 1800 {
            return None;
        }
        let mut output = String::new();
        if state.ticks == 0 {
            output.push_str("retry: 3000\n\n");
            output.push_str(&format!(
                "event: ready\ndata: {}\n\n",
                json!({"cursor": state.cursor.clone()})
            ));
        }
        let (items, cursor) = match collect_events(&state).await {
            Ok(value) => value,
            Err(error) => {
                return Some((
                    Ok(format!(
                        "event: error\ndata: {}\n\n",
                        json!({"detail": error})
                    )),
                    None,
                ))
            }
        };
        for (stamp, event_id, event) in &items {
            if let Some(max) = state.max_events {
                if state.emitted >= max {
                    break;
                }
            }
            state.cursor = stamp.clone();
            state.emitted += 1;
            output.push_str(&frame(event, event_id));
        }
        state.cursor = cursor;
        state.ticks += 1;
        if output.is_empty() {
            output.push_str(": keep-alive\n\n");
        }
        if state.max_events.is_some() {
            return Some((Ok(output), None));
        }
        if state.ticks > 1800 {
            return Some((Ok(output), None));
        }
        match state.listener.as_mut() {
            Some(listener) => {
                await_scope_notification(listener, &state.tenant_id, &state.project_id).await
            }
            None => tokio::time::sleep(Duration::from_secs(2)).await,
        }
        Some((Ok(output), Some(state)))
    })
}

async fn socket_auth_headers(headers: &HeaderMap) -> HeaderMap {
    let mut auth_headers = headers.clone();
    if let Some(protocols) = headers
        .get("sec-websocket-protocol")
        .and_then(|value| value.to_str().ok())
    {
        let token = protocols
            .split(',')
            .map(str::trim)
            .find_map(|protocol| protocol.strip_prefix("session."))
            .map(str::trim)
            .filter(|token| !token.is_empty() && token.len() <= 128);
        if let Some(token) = token {
            if let Ok(header_value) = token.parse() {
                auth_headers.insert("x-session-token", header_value);
            }
        }
    }
    auth_headers
}

/// Native replacement for the project WebSocket event channel. The HTTP SSE
/// endpoint remains the fallback for clients or intermediaries that cannot
/// complete the WebSocket handshake.
pub async fn events_socket(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Query(query): Query<StreamQuery>,
    upgrade: WebSocketUpgrade,
) -> Response {
    let auth_headers = socket_auth_headers(&headers).await;
    let user = match crate::billing_api::require_user(&state, &auth_headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error(error),
    } {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "Project not found."})),
        )
            .into_response();
    }
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(role) => role.unwrap_or_default(),
        Err(error) => return database_error(error),
    };
    let since_header = headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok());
    let cursor = parse_cursor(since_header.or(query.since.as_deref()));
    let stream_state = StreamState {
        pool: pool.clone(),
        postgres: is_postgres(&state),
        tenant_id,
        project_id,
        audit_visible: role == "owner" || role == "admin",
        cursor,
        max_events: None,
        emitted: 0,
        ticks: 0,
        listener: None,
    };
    upgrade
        .protocols(["aiagentstack"])
        .on_upgrade(move |socket| handle_socket(socket, stream_state))
}

async fn handle_socket(mut socket: WebSocket, state: StreamState) {
    let mut cursor = state.cursor.clone();
    let mut last_heartbeat = std::time::Instant::now();
    loop {
        let poll_state = state.at_cursor(&cursor);
        match collect_events(&poll_state).await {
            Ok((items, next)) => {
                for (stamp, _, event) in &items {
                    let message = json!({
                        "event": event.get("type").and_then(Value::as_str).unwrap_or("message"),
                        "data": event,
                        "cursor": stamp,
                    });
                    if socket
                        .send(Message::Text(message.to_string().into()))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
                cursor = next;
            }
            Err(error) => {
                let _ = socket
                    .send(Message::Text(
                        json!({"event": "error", "data": {"detail": error}})
                            .to_string()
                            .into(),
                    ))
                    .await;
                return;
            }
        }
        if last_heartbeat.elapsed() >= Duration::from_secs(15) {
            last_heartbeat = std::time::Instant::now();
            if socket
                .send(Message::Text(
                    json!({"event": "heartbeat"}).to_string().into(),
                ))
                .await
                .is_err()
            {
                return;
            }
        }
        tokio::select! {
            incoming = socket.recv() => match incoming {
                None => return,
                Some(Ok(Message::Text(text))) => {
                    if let Ok(Value::Object(object)) = serde_json::from_str::<Value>(&text) {
                        if let Some(Value::String(since)) = object.get("since") {
                            cursor = parse_cursor(Some(since));
                        }
                    }
                }
                Some(Ok(_)) => {}
                Some(Err(_)) => return,
            },
            _ = tokio::time::sleep(Duration::from_secs(1)) => {}
        }
    }
}

/// Native replacement for `GET /api/v1/tenants/{t}/projects/{p}/events/stream/`.
pub async fn events_stream(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Query(query): Query<StreamQuery>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error(error),
    } {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "Project not found."})),
        )
            .into_response();
    }
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(role) => role.unwrap_or_default(),
        Err(error) => return database_error(error),
    };
    let max_events = match query.max_events.as_deref() {
        None => None,
        Some(value) => match value.parse::<i64>() {
            Ok(number) if (1..=100).contains(&number) => Some(number),
            Ok(_) => return stream_error("max_events must be between 1 and 100."),
            Err(_) => return stream_error("max_events must be an integer."),
        },
    };
    let since_header = headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok());
    let cursor = parse_cursor(since_header.or(query.since.as_deref()));
    let postgres = is_postgres(&state);
    let listener = if postgres {
        match state.config.database_url.as_deref() {
            Some(url) => match sqlx::postgres::PgListener::connect(url).await {
                Ok(mut listener) => match listener.listen(CHANNEL).await {
                    Ok(()) => Some(listener),
                    Err(error) => {
                        tracing::warn!(%error, "realtime listener could not subscribe");
                        None
                    }
                },
                Err(error) => {
                    tracing::warn!(%error, "realtime listener could not connect");
                    None
                }
            },
            None => None,
        }
    } else {
        None
    };
    let stream_state = StreamState {
        pool: pool.clone(),
        postgres,
        tenant_id,
        project_id,
        audit_visible: role == "owner" || role == "admin",
        cursor,
        max_events,
        emitted: 0,
        ticks: 0,
        listener,
    };
    let mut response = Response::new(Body::from_stream(stream_body(stream_state)));
    response.headers_mut().insert(
        "content-type",
        "text/event-stream"
            .parse()
            .unwrap_or_else(|_| "text/plain".parse().expect("static header")),
    );
    response
        .headers_mut()
        .insert("cache-control", "no-cache".parse().expect("static header"));
    response
        .headers_mut()
        .insert("x-accel-buffering", "no".parse().expect("static header"));
    response
        .headers_mut()
        .insert("connection", "keep-alive".parse().expect("static header"));
    response
}
