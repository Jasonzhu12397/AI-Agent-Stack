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

fn run_json(row: &sqlx::any::AnyRow) -> Value {
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "team_version_id": row.try_get::<String, _>("team_version_id").unwrap_or_default(),
        "task_id": row.try_get::<Option<String>, _>("task_id").ok().flatten(),
        "task_status": row.try_get::<Option<String>, _>("task_status").ok().flatten(),
        "input_text": row.try_get::<String, _>("input_text").unwrap_or_default(),
        "output_text": row.try_get::<Option<String>, _>("output_text").ok().flatten(),
        "created_at": timestamp_value(row, "created_at"),
        "finished_at": timestamp_value(row, "finished_at"),
    })
}

async fn team_scope(pool: &sqlx::AnyPool, team_id: &str, user_id: i64) -> Result<bool, Response> {
    let team = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(team_id)
    .fetch_optional(pool)
    .await
    .map_err(|value| database_error(value.to_string()))?;
    let Some(row) = team else {
        return Err(error("Not found.", 404));
    };
    let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
    let project_id: String = row.try_get("project_id").unwrap_or_default();
    if !crate::billing_api::tenant_member(pool, &tenant_id, user_id)
        .await
        .unwrap_or(false)
    {
        return Err(error("Not found.", 404));
    }
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user_id).await {
        Ok(true) => Ok(true),
        Ok(false) => Err(error("Not found.", 404)),
        Err(_) => Err(unavailable()),
    }
}

/// `GET /api/v1/agent-teams/{team_id}/runs/`
pub async fn list_team_runs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(team_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = team_scope(pool, &team_id, user.id).await {
        return response;
    }
    let created = timestamp_expr(&state, "run", "created_at");
    let finished = timestamp_expr(&state, "run", "finished_at");
    let sql = format!(
        "SELECT CAST(run.id AS TEXT) AS id, CAST(run.team_version_id AS TEXT) AS team_version_id, \
         CAST(task.id AS TEXT) AS task_id, task.status AS task_status, run.input_text, run.output_text, \
         {created} AS created_at, {finished} AS finished_at \
         FROM core_agentteamrun run \
         JOIN core_agentteamversion version ON version.id = run.team_version_id \
         LEFT JOIN core_workflowrun workflow ON workflow.id = run.workflow_run_id \
         LEFT JOIN core_task task ON task.id = workflow.task_id \
         WHERE CAST(version.team_id AS TEXT) = ? ORDER BY run.created_at DESC"
    );
    let rows = match sqlx::query(&sql).bind(&team_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    Json(Value::Array(rows.iter().map(run_json).collect())).into_response()
}

/// `GET /api/v1/agent-team-runs/{run_id}/`
pub async fn team_run_detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let team_id: Option<String> = sqlx::query_scalar(
        "SELECT CAST(version.team_id AS TEXT) FROM core_agentteamrun run \
         JOIN core_agentteamversion version ON version.id = run.team_version_id \
         WHERE CAST(run.id AS TEXT) = ? LIMIT 1",
    )
    .bind(&run_id)
    .fetch_optional(pool)
    .await
    .unwrap_or(None);
    let Some(team_id) = team_id else {
        return error("Not found.", 404);
    };
    if let Err(response) = team_scope(pool, &team_id, user.id).await {
        return response;
    }
    let created = timestamp_expr(&state, "run", "created_at");
    let finished = timestamp_expr(&state, "run", "finished_at");
    let sql = format!(
        "SELECT CAST(run.id AS TEXT) AS id, CAST(run.team_version_id AS TEXT) AS team_version_id, \
         CAST(task.id AS TEXT) AS task_id, task.status AS task_status, run.input_text, run.output_text, \
         {created} AS created_at, {finished} AS finished_at \
         FROM core_agentteamrun run \
         LEFT JOIN core_workflowrun workflow ON workflow.id = run.workflow_run_id \
         LEFT JOIN core_task task ON task.id = workflow.task_id \
         WHERE CAST(run.id AS TEXT) = ? LIMIT 1"
    );
    match sqlx::query(&sql).bind(&run_id).fetch_optional(pool).await {
        Ok(Some(row)) => Json(run_json(&row)).into_response(),
        Ok(None) => error("Not found.", 404),
        Err(value) => database_error(value.to_string()),
    }
}

fn timestamp_expr(state: &AppState, alias: &str, name: &str) -> String {
    let postgres = state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"));
    if postgres {
        format!("to_char({alias}.{name} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%d %H:%M:%S', {alias}.{name})")
    }
}

fn config_error(code: &str, detail: &str, field: Option<&str>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"code": code, "detail": detail, "fields": field.map(|f| vec![f]).unwrap_or_default()})),
    )
        .into_response()
}

fn clean_instructions(value: Option<&Value>, field: &str) -> Result<String, Response> {
    match value {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(text)) if text.len() <= 4000 => Ok(text.trim().to_string()),
        Some(Value::String(_)) => Err(config_error(
            "TEAM_INSTRUCTIONS_TOO_LONG",
            &format!("{field} cannot exceed 4000 characters."),
            Some(field),
        )),
        Some(_) => Err(config_error(
            "TEAM_INSTRUCTIONS_INVALID",
            &format!("{field} must be a string."),
            Some(field),
        )),
    }
}

struct TeamAgent {
    name: String,
    version_id: String,
    version: i64,
    tool_allowlist: Vec<Value>,
}

fn resolve_member(
    agent_id: &str,
    field: &str,
    agents: &std::collections::HashMap<String, TeamAgent>,
    used: &mut std::collections::HashSet<String>,
) -> Result<TeamAgent, Response> {
    if !used.insert(agent_id.to_string()) {
        return Err(config_error(
            "TEAM_AGENT_DUPLICATE",
            "The same agent cannot appear twice in one team.",
            Some(field),
        ));
    }
    let Some(agent) = agents.get(agent_id) else {
        return Err(config_error(
            "TEAM_AGENT_UNAVAILABLE",
            &format!("{field} must reference an active agent in this project."),
            Some(field),
        ));
    };
    if !agent.tool_allowlist.is_empty() {
        return Err(config_error(
            "TEAM_AGENT_TOOLS_UNSUPPORTED",
            &format!("{field} grants plugin tools, which are not supported inside a team run yet. Publish a tool-free version for team use."),
            Some(field),
        ));
    }
    Ok(TeamAgent {
        name: agent.name.clone(),
        version_id: agent.version_id.clone(),
        version: agent.version,
        tool_allowlist: Vec::new(),
    })
}

fn member_node(index: usize, member: &Value, input_from: Option<&str>) -> Value {
    let role = member.get("role").and_then(Value::as_str).unwrap_or("");
    let instructions = member
        .get("instructions")
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut config = json!({
        "agent_version_id": member.get("agent_version_id").cloned().unwrap_or(Value::Null),
        "instructions": if role.is_empty() { instructions.to_string() } else { format!("Role: {role}.\n{instructions}") },
    });
    if let Some(source) = input_from {
        config["input_from"] = json!(source);
    }
    json!({"id": format!("member_{index}"), "type": "agent", "config": config})
}

fn build_graph(normalized: &Value) -> Value {
    let strategy = normalized
        .get("strategy")
        .and_then(Value::as_str)
        .unwrap_or("sequential");
    let members = normalized
        .get("members")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let coordinator = normalized
        .get("coordinator")
        .cloned()
        .unwrap_or(Value::Null);
    let mut nodes = vec![json!({"id": "start", "type": "start", "config": {}})];
    let mut edges = Vec::new();
    if matches!(strategy, "sequential" | "coordinator") {
        let mut previous = "start".to_string();
        for (offset, member) in members.iter().enumerate() {
            let node = member_node(
                offset + 1,
                member,
                if previous == "start" {
                    None
                } else {
                    Some(&previous)
                },
            );
            let id = node
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            nodes.push(node);
            edges.push(json!({"source": previous, "target": id}));
            previous = id;
        }
    } else if strategy == "parallel" {
        for (offset, member) in members.iter().enumerate() {
            let node = member_node(offset + 1, member, None);
            let id = node
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            nodes.push(node);
            edges.push(json!({"source": "start", "target": id}));
        }
    } else {
        let router = normalized.get("router").cloned().unwrap_or(Value::Null);
        let labels: Vec<String> = members
            .iter()
            .filter_map(|member| {
                member
                    .get("route_label")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .collect();
        let instructions = format!(
            "Role: router.\n{}\n\nReply with exactly one route label and no other text. Allowed labels: {}.",
            router.get("instructions").and_then(Value::as_str).unwrap_or(""),
            labels.join(", ")
        );
        nodes.push(json!({"id": "router", "type": "agent", "config": {
            "agent_version_id": router.get("agent_version_id").cloned().unwrap_or(Value::Null),
            "instructions": instructions,
        }}));
        edges.push(json!({"source": "start", "target": "router"}));
        let mut previous = "router".to_string();
        if members.len() > 1 {
            for index in 1..members.len() {
                let condition = format!("route_{index}");
                nodes.push(json!({"id": condition, "type": "condition", "config": {
                    "input_from": "router",
                    "equals": labels.get(index - 1).cloned().unwrap_or_default(),
                    "normalize": true,
                }}));
                edges.push(json!({"source": previous, "target": condition}));
                previous = condition;
            }
        }
        for (offset, member) in members.iter().enumerate() {
            let node = member_node(offset + 1, member, None);
            let id = node
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            nodes.push(node);
            if members.len() == 1 {
                edges.push(json!({"source": "router", "target": id}));
            } else if offset + 1 == members.len() {
                edges.push(
                    json!({"source": format!("route_{}", offset), "target": id, "when": false}),
                );
            } else {
                edges.push(
                    json!({"source": format!("route_{}", offset + 1), "target": id, "when": true}),
                );
            }
        }
    }
    if coordinator.is_object() {
        let gather: Vec<String> = (1..=members.len())
            .map(|index| format!("member_{index}"))
            .collect();
        nodes.push(json!({"id": "coordinator", "type": "agent", "config": {
            "agent_version_id": coordinator.get("agent_version_id").cloned().unwrap_or(Value::Null),
            "instructions": format!("Role: coordinator.\n{}", coordinator.get("instructions").and_then(Value::as_str).unwrap_or("")),
            "gather_from": gather,
        }}));
        for index in 1..=members.len() {
            edges.push(json!({"source": format!("member_{index}"), "target": "coordinator"}));
        }
        edges.push(json!({"source": "coordinator", "target": "end"}));
        nodes.push(json!({"id": "end", "type": "end", "config": {"source_node": "coordinator"}}));
    } else {
        for index in 1..=members.len() {
            edges.push(json!({"source": format!("member_{index}"), "target": "end"}));
        }
        nodes.push(json!({"id": "end", "type": "end", "config": {
            "gather_from": (1..=members.len()).map(|index| format!("member_{index}")).collect::<Vec<String>>()
        }}));
    }
    json!({"nodes": nodes, "edges": edges})
}

/// `POST /api/v1/agent-teams/{team_id}/publish/`
pub async fn publish_team(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(team_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let team = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, name, \
         CASE WHEN is_archived THEN 1 ELSE 0 END AS is_archived, draft_config \
         FROM core_agentteam WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&team_id)
    .fetch_optional(pool)
    .await;
    let team = match team {
        Ok(Some(row)) => row,
        Ok(None) => return error("Not found.", 404),
        Err(value) => return database_error(value.to_string()),
    };
    let tenant_id: String = team.try_get("tenant_id").unwrap_or_default();
    let project_id: String = team.try_get("project_id").unwrap_or_default();
    let editable = match scope(pool, &tenant_id, &project_id, user.id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    if !editable {
        return error("Project editor permission required.", 403);
    }
    if team.try_get::<i64, _>("is_archived").unwrap_or(0) != 0 {
        return error("Archived teams cannot be published.", 409);
    }
    let draft: Value = team
        .try_get::<Option<String>, _>("draft_config")
        .ok()
        .flatten()
        .and_then(|value| serde_json::from_str(&value).ok())
        .unwrap_or(json!({}));
    let Some(draft) = draft.as_object() else {
        return config_error(
            "TEAM_CONFIGURATION_INVALID",
            "Team configuration must be a JSON object.",
            None,
        );
    };
    let strategy = draft
        .get("strategy")
        .and_then(Value::as_str)
        .unwrap_or("sequential");
    if !matches!(
        strategy,
        "sequential" | "coordinator" | "parallel" | "router"
    ) {
        return config_error(
            "TEAM_STRATEGY_INVALID",
            "strategy must be sequential, coordinator, parallel or router.",
            Some("strategy"),
        );
    }
    let members = draft
        .get("members")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if members.is_empty() || members.len() > 6 {
        return config_error(
            "TEAM_MEMBERS_INVALID",
            "members must contain 1 to 6 agents.",
            Some("members"),
        );
    }
    let agents_rows = sqlx::query(
        "SELECT CAST(definition.id AS TEXT) AS id, definition.name, definition.current_version, \
         CASE WHEN definition.is_archived THEN 1 ELSE 0 END AS is_archived, \
         CAST(version.id AS TEXT) AS version_id, version.version AS agent_version, version.config_snapshot \
         FROM core_agentdefinition definition \
         JOIN core_agentversion version ON CAST(version.agent_id AS TEXT) = CAST(definition.id AS TEXT) \
           AND version.version = definition.current_version \
         WHERE CAST(definition.tenant_id AS TEXT) = ? AND CAST(definition.project_id AS TEXT) = ?",
    )
    .bind(&tenant_id)
    .bind(&project_id)
    .fetch_all(pool)
    .await;
    let agents_rows = match agents_rows {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    let mut agents = std::collections::HashMap::new();
    for row in agents_rows {
        if row.try_get::<i64, _>("is_archived").unwrap_or(0) != 0 {
            continue;
        }
        let snapshot: Value = row
            .try_get::<Option<String>, _>("config_snapshot")
            .ok()
            .flatten()
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or(json!({}));
        let allowlist = snapshot
            .get("tool_allowlist")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let id = row.try_get::<String, _>("id").unwrap_or_default();
        agents.insert(
            id,
            TeamAgent {
                name: row.try_get::<String, _>("name").unwrap_or_default(),
                version_id: row.try_get::<String, _>("version_id").unwrap_or_default(),
                version: row.try_get::<i64, _>("agent_version").unwrap_or(0),
                tool_allowlist: allowlist,
            },
        );
    }
    let mut used = std::collections::HashSet::new();
    let mut normalized_members = Vec::new();
    let mut route_labels = std::collections::HashSet::new();
    for (index, member) in members.iter().enumerate() {
        let Some(member) = member.as_object() else {
            return config_error(
                "TEAM_MEMBER_INVALID",
                &format!("members[{index}] must be an object."),
                Some(&format!("members[{index}]")),
            );
        };
        let field = format!("members[{index}].agent_id");
        let agent_id = member.get("agent_id").and_then(Value::as_str).unwrap_or("");
        if uuid::Uuid::parse_str(agent_id).is_err() {
            return config_error(
                "TEAM_MEMBER_INVALID",
                &format!("{field} must be a UUID."),
                Some(&field),
            );
        }
        let agent = match resolve_member(agent_id, &field, &agents, &mut used) {
            Ok(agent) => agent,
            Err(response) => return response,
        };
        let role = member.get("role").and_then(Value::as_str).unwrap_or("");
        if role.len() > 80 {
            return config_error(
                "TEAM_ROLE_INVALID",
                &format!("members[{index}].role must be at most 80 characters."),
                Some(&format!("members[{index}].role")),
            );
        }
        let instructions = match clean_instructions(
            member.get("instructions"),
            &format!("members[{index}].instructions"),
        ) {
            Ok(value) => value,
            Err(response) => return response,
        };
        if instructions.is_empty() {
            return config_error(
                "TEAM_INSTRUCTIONS_MISSING",
                &format!("members[{index}].instructions must describe the member's task."),
                Some(&format!("members[{index}].instructions")),
            );
        }
        let mut route_label = String::new();
        if strategy == "router" {
            let label = member
                .get("route_label")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_lowercase();
            if label.is_empty()
                || label.len() > 40
                || !label
                    .chars()
                    .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-'))
            {
                return config_error("TEAM_ROUTE_LABEL_INVALID", &format!("members[{index}].route_label must be 1 to 40 characters using letters, numbers, underscore or hyphen."), Some(&format!("members[{index}].route_label")));
            }
            if !route_labels.insert(label.clone()) {
                return config_error(
                    "TEAM_ROUTE_LABEL_DUPLICATE",
                    "Every router member needs a unique route_label.",
                    Some(&format!("members[{index}].route_label")),
                );
            }
            route_label = label;
        }
        normalized_members.push(json!({
            "agent_id": agent_id,
            "agent_version_id": agent.version_id,
            "agent_name": agent.name,
            "agent_version": agent.version,
            "role": role.trim(),
            "instructions": instructions,
            "route_label": route_label,
            "order": index,
        }));
    }
    let mut coordinator = Value::Null;
    let coordinator_config = draft.get("coordinator").cloned().unwrap_or(Value::Null);
    if matches!(strategy, "coordinator" | "parallel") && coordinator_config.is_null() {
        return config_error(
            "TEAM_COORDINATOR_MISSING",
            &format!("{strategy} strategy requires a coordinator object."),
            Some("coordinator"),
        );
    }
    if strategy == "sequential"
        && !coordinator_config.is_null()
        && coordinator_config != json!({})
        && coordinator_config != json!([])
    {
        return config_error(
            "TEAM_COORDINATOR_NOT_ALLOWED",
            "sequential strategy cannot define a coordinator.",
            Some("coordinator"),
        );
    }
    if !coordinator_config.is_null()
        && coordinator_config != json!({})
        && coordinator_config != json!([])
    {
        let field = "coordinator.agent_id";
        let agent_id = coordinator_config
            .get("agent_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let agent = match resolve_member(agent_id, field, &agents, &mut used) {
            Ok(agent) => agent,
            Err(response) => return response,
        };
        let instructions = match clean_instructions(
            coordinator_config.get("instructions"),
            "coordinator.instructions",
        ) {
            Ok(value) => value,
            Err(response) => return response,
        };
        if instructions.is_empty() {
            return config_error(
                "TEAM_INSTRUCTIONS_MISSING",
                "coordinator.instructions must describe its task.",
                Some("coordinator.instructions"),
            );
        }
        coordinator = json!({"agent_id": agent_id, "agent_version_id": agent.version_id, "agent_name": agent.name, "agent_version": agent.version, "instructions": instructions, "role": "coordinator", "order": normalized_members.len()});
    }
    let mut router = Value::Null;
    let router_config = draft.get("router").cloned().unwrap_or(Value::Null);
    if strategy == "router" {
        if router_config.is_null() {
            return config_error(
                "TEAM_ROUTER_MISSING",
                "router strategy requires a router object.",
                Some("router"),
            );
        }
        let agent_id = router_config
            .get("agent_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let agent = match resolve_member(agent_id, "router.agent_id", &agents, &mut used) {
            Ok(agent) => agent,
            Err(response) => return response,
        };
        let instructions =
            match clean_instructions(router_config.get("instructions"), "router.instructions") {
                Ok(value) => value,
                Err(response) => return response,
            };
        if instructions.is_empty() {
            return config_error(
                "TEAM_INSTRUCTIONS_MISSING",
                "router.instructions must describe its task.",
                Some("router.instructions"),
            );
        }
        router = json!({"agent_id": agent_id, "agent_version_id": agent.version_id, "agent_name": agent.name, "agent_version": agent.version, "instructions": instructions, "role": "router", "order": -1});
    } else if !router_config.is_null() && router_config != json!({}) && router_config != json!([]) {
        return config_error(
            "TEAM_ROUTER_NOT_ALLOWED",
            "router is only valid with the router strategy.",
            Some("router"),
        );
    }
    let normalized_members_value = Value::Array(normalized_members);
    let normalized = json!({"strategy": strategy, "members": normalized_members_value, "coordinator": coordinator, "router": router});
    let graph = build_graph(&normalized);
    let team_name: String = team.try_get("name").unwrap_or_default();
    let workflow_version_id = uuid::Uuid::new_v4().to_string();
    let version_id = uuid::Uuid::new_v4().to_string();
    let slug = format!("agent-team-{}", team_id.replace('-', ""))
        .chars()
        .take(80)
        .collect::<String>();
    let existing_workflow: Option<String> = sqlx::query_scalar(
        "SELECT CAST(id AS TEXT) FROM core_workflowdefinition WHERE CAST(project_id AS TEXT) = ? AND slug = ? LIMIT 1",
    )
    .bind(&project_id)
    .bind(&slug)
    .fetch_optional(pool)
    .await
    .unwrap_or(None);
    let workflow_id = existing_workflow.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let workflow_version: i64 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(version), 0) FROM core_workflowversion WHERE CAST(workflow_id AS TEXT) = ?",
    )
    .bind(&workflow_id)
    .fetch_one(pool)
    .await
    .unwrap_or(0)
        + 1;
    let team_version: i64 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(version), 0) FROM core_agentteamversion WHERE CAST(team_id AS TEXT) = ?",
    )
    .bind(&team_id)
    .fetch_one(pool)
    .await
    .unwrap_or(0)
        + 1;
    let mut transaction = match pool.begin().await {
        Ok(value) => value,
        Err(value) => return database_error(value.to_string()),
    };
    let definition = sqlx::query(
        "INSERT INTO core_workflowdefinition \
         (id, tenant_id, project_id, created_by_id, name, slug, description, draft_definition, current_version, is_archived, created_at, updated_at) \
         SELECT ?, ?, ?, ?, ?, ?, 'Generated from an agent team definition.', ?, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP \
         WHERE NOT EXISTS (SELECT 1 FROM core_workflowdefinition WHERE CAST(project_id AS TEXT) = ? AND slug = ?)",
    )
    .bind(&workflow_id)
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(user.id)
    .bind(format!("Agent team: {team_name}").chars().take(120).collect::<String>())
    .bind(&slug)
    .bind(graph.to_string())
    .bind(&project_id)
    .bind(&slug)
    .execute(&mut *transaction)
    .await;
    if let Err(value) = definition {
        let _ = transaction.rollback().await;
        return database_error(value.to_string());
    }
    if definition.unwrap().rows_affected() == 0 {
        let _ = transaction.rollback().await;
        return database_error("Workflow definition already exists.".into());
    }
    if let Err(value) = sqlx::query(
        "INSERT INTO core_workflowversion (id, workflow_id, version, definition_snapshot, created_by_id, published_at) \
         VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
    )
    .bind(&workflow_version_id)
    .bind(&workflow_id)
    .bind(workflow_version)
    .bind(graph.to_string())
    .bind(user.id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(value.to_string());
    }
    let snapshot = json!({
        "strategy": strategy,
        "members": normalized.get("members").cloned().unwrap_or(json!([])),
        "coordinator": normalized.get("coordinator").cloned().unwrap_or(Value::Null),
        "router": normalized.get("router").cloned().unwrap_or(Value::Null),
        "workflow_definition_id": workflow_id,
        "workflow_version": workflow_version,
    });
    if let Err(value) = sqlx::query(
        "INSERT INTO core_agentteamversion (id, team_id, version, definition_snapshot, workflow_version_id, created_by_id, published_at) \
         VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
    )
    .bind(&version_id)
    .bind(&team_id)
    .bind(team_version)
    .bind(snapshot.to_string())
    .bind(&workflow_version_id)
    .bind(user.id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(value.to_string());
    }
    if let Err(value) = sqlx::query(
        "UPDATE core_agentteam SET current_version = ?, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
    )
    .bind(team_version)
    .bind(&team_id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(value.to_string());
    }
    if let Err(value) = transaction.commit().await {
        return database_error(value.to_string());
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or_default(),
        crate::audit::AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "agent_team.published",
            resource_type: "agent_team_version",
            resource_id: &version_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: headers.get("x-request-id").and_then(|value| value.to_str().ok()).unwrap_or_default(),
            remote_addr: None,
            user_agent: headers.get("user-agent").and_then(|value| value.to_str().ok()).unwrap_or_default(),
            metadata: json!({"team_id": team_id, "version": team_version, "workflow_version_id": workflow_version_id, "strategy": strategy, "member_count": normalized.get("members").and_then(Value::as_array).map(Vec::len).unwrap_or(0)}),
        },
    )
    .await;
    (
        StatusCode::CREATED,
        Json(json!({
            "id": version_id,
            "team_id": team_id,
            "version": team_version,
            "definition_snapshot": snapshot,
            "workflow_version_id": workflow_version_id,
            "created_by": user.id,
            "published_at": null,
        })),
    )
        .into_response()
}
