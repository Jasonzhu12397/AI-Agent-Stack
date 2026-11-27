#![allow(clippy::result_large_err)]

use std::time::Duration;

use axum::body::Body;
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

#[derive(Deserialize, Default)]
pub struct StreamQuery {
    since: Option<String>,
    max_events: Option<String>,
}

#[derive(Clone)]
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
        format!("strftime('%Y-%m-%d %H:%M:%S.%f', {column}) AS {column}")
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
        tokio::time::sleep(Duration::from_secs(2)).await;
        Some((Ok(output), Some(state)))
    })
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
    let stream_state = StreamState {
        pool: pool.clone(),
        postgres: is_postgres(&state),
        tenant_id,
        project_id,
        audit_visible: role == "owner" || role == "admin",
        cursor,
        max_events,
        emitted: 0,
        ticks: 0,
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
