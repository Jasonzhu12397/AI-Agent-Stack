#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::Row;

use crate::proxy::AppState;

fn error(message: &str, status: u16) -> Response {
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST),
        Json(json!({"detail": message})),
    )
        .into_response()
}

fn unavailable() -> Response {
    error("The service is temporarily unavailable.", 503)
}

fn database_error(value: String) -> Response {
    tracing::error!(%value, "rust agent team api database operation failed");
    unavailable()
}

fn timestamp_value(row: &sqlx::any::AnyRow, column: &str) -> Value {
    let text: Option<String> = row.try_get(column).ok().flatten();
    text.map(|value| json!(value)).unwrap_or(Value::Null)
}

fn team_json(row: &sqlx::any::AnyRow) -> Value {
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
        "name": row.try_get::<String, _>("name").unwrap_or_default(),
        "slug": row.try_get::<String, _>("slug").unwrap_or_default(),
        "description": row.try_get::<String, _>("description").unwrap_or_default(),
        "draft_config": row
            .try_get::<String, _>("draft_text")
            .ok()
            .and_then(|value| serde_json::from_str::<Value>(&value).ok())
            .unwrap_or_else(|| json!({})),
        "current_version": row.try_get::<i64, _>("current_version").unwrap_or(0),
        "is_archived": row.try_get::<i64, _>("is_archived").unwrap_or(0) != 0,
        "created_at": timestamp_value(row, "created_at"),
        "updated_at": timestamp_value(row, "updated_at"),
    })
}

async fn scope(
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<bool, Response> {
    if !crate::billing_api::tenant_member(pool, tenant_id, user_id)
        .await
        .unwrap_or(false)
    {
        return Err(error("Not found.", 404));
    }
    let exists = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_project WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
    )
    .bind(project_id)
    .bind(tenant_id)
    .fetch_one(pool)
    .await
    .map_err(|value| database_error(value.to_string()))?;
    if exists == 0 {
        return Err(error("Not found.", 404));
    }
    let admin = crate::billing_api::tenant_admin(pool, tenant_id, user_id)
        .await
        .unwrap_or(false);
    if admin {
        return Ok(true);
    }
    let role: Option<String> = sqlx::query_scalar(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|value| database_error(value.to_string()))?;
    match role.as_deref() {
        Some("admin" | "editor") => Ok(true),
        Some(_) => Ok(false),
        None => Err(error("Not found.", 404)),
    }
}

fn team_columns(state: &AppState) -> String {
    let ts = |column: &str| {
        if is_postgres(state) {
            format!("to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS {column}")
        } else {
            format!("strftime('%Y-%m-%d %H:%M:%S', {column}) AS {column}")
        }
    };
    [
        "CAST(id AS TEXT) AS id".to_string(),
        "CAST(tenant_id AS TEXT) AS tenant_id".to_string(),
        "CAST(project_id AS TEXT) AS project_id".to_string(),
        "name, slug, description".to_string(),
        "CAST(draft_config AS TEXT) AS draft_text".to_string(),
        "CAST(current_version AS INTEGER) AS current_version".to_string(),
        "CASE WHEN is_archived THEN 1 ELSE 0 END AS is_archived".to_string(),
        ts("created_at"),
        ts("updated_at"),
    ]
    .join(", ")
}

pub async fn list_teams(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = scope(pool, &tenant_id, &project_id, user.id).await {
        return response;
    }
    let select = format!(
        "SELECT {} FROM core_agentteam WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         AND NOT is_archived ORDER BY name",
        team_columns(&state)
    );
    let rows = match sqlx::query(&select)
        .bind(&tenant_id)
        .bind(&project_id)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    Json(rows.iter().map(team_json).collect::<Vec<_>>()).into_response()
}

pub async fn create_team(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Json(payload): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match scope(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return error("Project editor permission required.", 403),
        Err(response) => return response,
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if name.is_empty() || name.chars().count() > 120 {
        return error("name is required and must be at most 120 characters.", 400);
    }
    let slug = payload
        .get("slug")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if slug.is_empty() || slug.chars().count() > 80 {
        return error("slug is required and must be at most 80 characters.", 400);
    }
    let draft = payload
        .get("draft_config")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !draft.is_object() {
        return error("Team configuration must be a JSON object.", 400);
    }
    if draft.to_string().len() > 32768 {
        return error("Team configuration exceeds 32 KiB.", 400);
    }
    if contains_secret(&draft) {
        return error(
            "Credentials must use credential references, not team configuration.",
            400,
        );
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let now = timestamp();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let sql = format!(
        "INSERT INTO core_agentteam (id, tenant_id, project_id, created_by_id, name, slug, description, \
         draft_config, current_version, is_archived, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, false, {now_expr}, {now_expr})"
    );
    let description = payload
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    match sqlx::query(&sql)
        .bind(&id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(name)
        .bind(slug)
        .bind(description)
        .bind(draft.to_string())
        .execute(pool)
        .await
    {
        Ok(_) => {}
        Err(value) => {
            let text = value.to_string();
            if text.contains("UNIQUE") || text.contains("unique") {
                return error("A team with this slug already exists in the project.", 409);
            }
            return database_error(text);
        }
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: Some(project_id.as_str()),
            event_type: "agent_team.created",
            resource_type: "agent_team",
            resource_id: id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"slug": slug, "name": name, "project_id": project_id}),
        },
    )
    .await;
    let select = format!(
        "SELECT {} FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
        team_columns(&state)
    );
    match sqlx::query(&select).bind(&id).fetch_optional(pool).await {
        Ok(Some(row)) => (StatusCode::CREATED, Json(team_json(&row))).into_response(),
        Ok(None) => error("Team not found.", 404),
        Err(value) => database_error(value.to_string()),
    }
}

pub async fn team_detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(team_id): Path<String>,
    method: axum::http::Method,
    payload: Option<Json<Value>>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let tenant_row = match sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, \
         CASE WHEN is_archived THEN 1 ELSE 0 END AS is_archived \
         FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&team_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return error("Not found.", 404),
        Err(value) => return database_error(value.to_string()),
    };
    let tenant_id: String = tenant_row.try_get("tenant_id").unwrap_or_default();
    let project_id: String = tenant_row.try_get("project_id").unwrap_or_default();
    let editable = match scope(pool, &tenant_id, &project_id, user.id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    if method == axum::http::Method::GET {
        let select = format!(
            "SELECT {} FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
            team_columns(&state)
        );
        return match sqlx::query(&select)
            .bind(&team_id)
            .fetch_optional(pool)
            .await
        {
            Ok(Some(row)) => Json(team_json(&row)).into_response(),
            Ok(None) => error("Not found.", 404),
            Err(value) => database_error(value.to_string()),
        };
    }
    if !editable {
        return error("Project editor permission required.", 403);
    }
    let archived: i64 = tenant_row.try_get("is_archived").unwrap_or(0);
    if archived != 0 {
        return error("Archived teams are read-only.", 409);
    }
    let body = payload
        .map(|Json(value)| value)
        .unwrap_or_else(|| json!({}));
    let mut updates: Vec<String> = Vec::new();
    let mut bindings: Vec<String> = Vec::new();
    for (field, column) in [
        ("name", "name"),
        ("slug", "slug"),
        ("description", "description"),
        ("draft_config", "draft_config"),
    ] {
        let Some(value) = body.get(field) else {
            continue;
        };
        if field == "draft_config" {
            if !value.is_object() {
                return error("Team configuration must be a JSON object.", 400);
            }
            if value.to_string().len() > 32768 {
                return error("Team configuration exceeds 32 KiB.", 400);
            }
            if contains_secret(value) {
                return error(
                    "Credentials must use credential references, not team configuration.",
                    400,
                );
            }
            updates.push(format!("{column} = ?"));
            bindings.push(value.to_string());
        } else {
            let text = value.as_str().unwrap_or("");
            updates.push(format!("{column} = ?"));
            bindings.push(text.to_string());
        }
    }
    if updates.is_empty() {
        return error("No supported fields were provided.", 400);
    }
    let now = timestamp();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let sql = format!(
        "UPDATE core_agentteam SET {}, updated_at = {now_expr} WHERE CAST(id AS TEXT) = ?",
        updates.join(", ")
    );
    let mut query = sqlx::query(&sql);
    for value in &bindings {
        query = query.bind(value);
    }
    if let Err(value) = query.bind(&team_id).execute(pool).await {
        return database_error(value.to_string());
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: Some(project_id.as_str()),
            event_type: "agent_team.updated",
            resource_type: "agent_team",
            resource_id: team_id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"fields": body.as_object().map(|value| value.keys().cloned().collect::<Vec<_>>()).unwrap_or_default() }),
        },
    )
    .await;
    let select = format!(
        "SELECT {} FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
        team_columns(&state)
    );
    match sqlx::query(&select)
        .bind(&team_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => Json(team_json(&row)).into_response(),
        Ok(None) => error("Not found.", 404),
        Err(value) => database_error(value.to_string()),
    }
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(key, child)| {
            matches!(
                key.to_lowercase().as_str(),
                "api_key" | "secret" | "password" | "access_token" | "client_secret"
            ) || contains_secret(child)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        _ => false,
    }
}

fn is_postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"))
}

fn timestamp() -> String {
    chrono::Utc::now().format("%Y-%m-%d %H:%M:%S").to_string()
}
