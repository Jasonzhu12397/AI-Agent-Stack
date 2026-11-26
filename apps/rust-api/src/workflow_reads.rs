#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access};

/// Native replacement for the workflow catalog.
pub async fn catalog(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let models = match catalog_models(pool, &state, &tenant_id, user.id).await {
        Ok(items) => items,
        Err(error) => return database_error_message(error),
    };
    let knowledge_bases = match catalog_knowledge_bases(pool, &state, &tenant_id, &project_id).await
    {
        Ok(items) => items,
        Err(error) => return database_error_message(error),
    };
    let tools = match catalog_tools(pool, &state, &tenant_id, &project_id).await {
        Ok(items) => items,
        Err(error) => return database_error_message(error),
    };
    let agents = match catalog_agents(pool, &state, &tenant_id, &project_id).await {
        Ok(items) => items,
        Err(error) => return database_error_message(error),
    };
    Json(json!({
        "models": models,
        "knowledge_bases": knowledge_bases,
        "tools": tools,
        "agents": agents,
    }))
    .into_response()
}

/// Native replacement for the workflow run listing.
pub async fn list_runs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(workflow_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::workflows::workflow_access(pool, &state, &workflow_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let postgres = is_postgres(&state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(r.id AS TEXT) AS id, CAST(r.task_id AS TEXT) AS task_id, \
         CAST(r.workflow_version_id AS TEXT) AS workflow_version_id, \
         t.status AS task_status, t.error{json_cast} AS task_error_text, \
         r.input_data{json_cast} AS input_text, r.state{json_cast} AS state_text, \
         r.output_data{json_cast} AS output_text, \
         {deadline}, {created}, {finished} \
         FROM core_workflowrun r \
         JOIN core_task t ON t.id = r.task_id \
         JOIN core_workflowversion v ON v.id = r.workflow_version_id \
         WHERE CAST(v.workflow_id AS TEXT) = ? \
         ORDER BY r.created_at DESC",
        deadline = timestamp_alias(postgres, "r", "deadline_at", "deadline"),
        created = timestamp_alias(postgres, "r", "created_at", "created"),
        finished = timestamp_alias(postgres, "r", "finished_at", "finished"),
    );
    let rows = match sqlx::query(&sql).bind(&workflow_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error.to_string()),
    };
    let mut runs = Vec::with_capacity(rows.len());
    for row in &rows {
        let run_id: String = match row.try_get("id") {
            Ok(value) => value,
            Err(error) => return database_error_message(error.to_string()),
        };
        let steps = match run_steps(pool, &state, &run_id).await {
            Ok(steps) => steps,
            Err(error) => return database_error_message(error),
        };
        runs.push(json!({
            "id": run_id,
            "task_id": row.try_get::<String, _>("task_id").unwrap_or_default(),
            "workflow_version_id": row.try_get::<String, _>("workflow_version_id").unwrap_or_default(),
            "task_status": row.try_get::<String, _>("task_status").unwrap_or_default(),
            "task_error": json_text(row.try_get::<Option<String>, _>("task_error_text").ok().flatten()),
            "input_data": json_text(row.try_get::<Option<String>, _>("input_text").ok().flatten()),
            "state": json_text(row.try_get::<Option<String>, _>("state_text").ok().flatten()),
            "output_data": json_text(row.try_get::<Option<String>, _>("output_text").ok().flatten()),
            "deadline_at": row.try_get::<Option<String>, _>("deadline").ok().flatten().as_deref().map(normalize_timestamp),
            "steps": steps,
            "created_at": row.try_get::<Option<String>, _>("created").ok().flatten().as_deref().map(normalize_timestamp),
            "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten().as_deref().map(normalize_timestamp),
        }));
    }
    Json(runs).into_response()
}

async fn catalog_models(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let sql = format!(
        "SELECT CAST(config.id AS TEXT) AS id, config.display_name AS label, \
         provider.name AS provider, config.model_name AS model_name \
         FROM core_modelconfig config \
         JOIN core_modelprovider provider ON provider.id = config.provider_id \
         WHERE (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?) \
           AND provider.enabled = {true_literal} AND config.enabled = {true_literal} \
         ORDER BY config.display_name"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "label": row.try_get::<String, _>("label").unwrap_or_default(),
                "provider": row.try_get::<String, _>("provider").unwrap_or_default(),
                "model_name": row.try_get::<String, _>("model_name").unwrap_or_default(),
            })
        })
        .collect())
}

async fn catalog_knowledge_bases(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Value>, String> {
    let false_literal = if is_postgres(state) { "FALSE" } else { "0" };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, name FROM core_knowledgebase \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
           AND is_archived = {false_literal} ORDER BY name"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(project_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
            })
        })
        .collect())
}

async fn catalog_tools(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT plugin.plugin_key AS plugin_key, plugin.capabilities{json_cast} AS capabilities_text, \
         plugin.manifest{json_cast} AS manifest_text, \
         installation.granted_capabilities{json_cast} AS granted_text \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin ON plugin.id = installation.plugin_id \
         WHERE CAST(installation.tenant_id AS TEXT) = ? \
           AND CAST(installation.project_id AS TEXT) = ? \
           AND installation.status = 'installed' \
           AND plugin.enabled = {true_literal} \
           AND installation.plugin_version = plugin.version \
         ORDER BY plugin.name"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(project_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    let mut items = Vec::new();
    for row in &rows {
        let granted: Vec<String> = row
            .try_get::<Option<String>, _>("granted_text")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Vec<Value>>(&text).ok())
            .map(|values| {
                values
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let capabilities = row
            .try_get::<Option<String>, _>("capabilities_text")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or_else(|| json!([]));
        let manifest = row
            .try_get::<Option<String>, _>("manifest_text")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or_else(|| json!({}));
        let declarations = declared_capabilities(&manifest, &capabilities);
        let plugin_key = row.try_get::<String, _>("plugin_key").unwrap_or_default();
        for capability in capabilities
            .as_array()
            .map(|values| values.as_slice())
            .unwrap_or(&[])
        {
            let name = match capability {
                Value::String(name) => Some(name.clone()),
                Value::Object(map) => map.get("name").and_then(Value::as_str).map(str::to_string),
                _ => None,
            };
            let Some(name) = name else {
                continue;
            };
            if !granted.contains(&name) {
                continue;
            }
            let Some(definition) = declarations
                .iter()
                .find(|entry| entry.get("name").and_then(Value::as_str) == Some(name.as_str()))
            else {
                continue;
            };
            items.push(json!({
                "id": name,
                "name": name,
                "risk_level": definition.get("risk_level").and_then(Value::as_str).unwrap_or("low"),
                "plugin": plugin_key,
            }));
        }
    }
    Ok(items)
}

fn declared_capabilities(manifest: &Value, legacy: &Value) -> Vec<Value> {
    if let Some(entries) = manifest.get("capabilities").and_then(Value::as_array) {
        if !entries.is_empty() {
            return entries
                .iter()
                .filter(|entry| entry.is_object())
                .cloned()
                .collect();
        }
    }
    legacy
        .as_array()
        .map(|entries| {
            entries
                .iter()
                .filter_map(|entry| match entry {
                    Value::Object(map) => {
                        let name = map.get("name").and_then(Value::as_str)?.to_string();
                        let mut output = serde_json::Map::new();
                        output.insert("name".to_string(), json!(name));
                        output.insert("risk_level".to_string(), json!("low"));
                        for (key, value) in map {
                            output.insert(key.clone(), value.clone());
                        }
                        Some(Value::Object(output))
                    }
                    Value::String(name) => Some(json!({"name": name, "risk_level": "low"})),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

async fn catalog_agents(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let sql = format!(
        "SELECT CAST(version.id AS TEXT) AS id, agent.name AS name, version.version AS version \
         FROM core_agentversion version \
         JOIN core_agentdefinition agent ON agent.id = version.agent_id \
         WHERE CAST(agent.tenant_id AS TEXT) = ? AND CAST(agent.project_id AS TEXT) = ? \
           AND agent.is_archived = {false_literal} \
         ORDER BY version.published_at DESC",
        false_literal = if postgres { "FALSE" } else { "0" },
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(project_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "version": row.try_get::<i64, _>("version").unwrap_or_default(),
            })
        })
        .collect())
}

async fn run_steps(pool: &AnyPool, state: &AppState, run_id: &str) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, node_id, node_type, status, \
         input_data{json_cast} AS input_text, output_data{json_cast} AS output_text, \
         error{json_cast} AS error_text, attempt, {started}, {finished} \
         FROM core_workflowsteprun WHERE CAST(workflow_run_id AS TEXT) = ? ORDER BY created_at",
        started =
            crate::tenancy::timestamp_field_expr(postgres, "core_workflowsteprun", "started_at"),
        finished =
            crate::tenancy::timestamp_field_expr(postgres, "core_workflowsteprun", "finished_at"),
    );
    let rows = sqlx::query(&sql)
        .bind(run_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "node_id": row.try_get::<String, _>("node_id").unwrap_or_default(),
                "node_type": row.try_get::<String, _>("node_type").unwrap_or_default(),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "input_data": json_text(row.try_get::<Option<String>, _>("input_text").ok().flatten()),
                "output_data": json_text(row.try_get::<Option<String>, _>("output_text").ok().flatten()),
                "error": json_text(row.try_get::<Option<String>, _>("error_text").ok().flatten()),
                "attempt": row.try_get::<i64, _>("attempt").unwrap_or(1),
                "started_at": row.try_get::<Option<String>, _>("started").ok().flatten().as_deref().map(normalize_timestamp),
                "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten().as_deref().map(normalize_timestamp),
            })
        })
        .collect())
}

fn timestamp_alias(postgres: bool, table: &str, field: &str, alias: &str) -> String {
    if postgres {
        format!(
            "to_char({table}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS {alias}"
        )
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {table}.{field}) AS {alias}")
    }
}

fn json_text(value: Option<String>) -> Value {
    value
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or(Value::Null)
}

async fn require_user(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<crate::context::SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return Err(unavailable());
    };
    match authenticate(pool, secret_key, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(message)) => {
            Err((StatusCode::UNAUTHORIZED, Json(json!({"detail": message}))).into_response())
        }
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust workflow read api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"detail": "Not found."}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust workflow read database operation failed");
    unavailable()
}

/// Native replacement for the workflow run creation endpoint.
pub async fn create_run(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(workflow_id): Path<String>,
    body: axum::body::Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let workflow =
        match crate::workflows::workflow_access(pool, &state, &workflow_id, user.id).await {
            Ok(Some(record)) => record,
            Ok(None) => return not_found(),
            Err(error) => return database_error_message(error),
        };
    if workflow.is_archived || workflow.current_version <= 0 {
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Workflow must have a published version before it can run."})),
        )
            .into_response();
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let input_data = payload.get("input").cloned().unwrap_or_else(|| json!({}));
    if !input_data.is_object() || input_data.to_string().len() > 65536 {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "input must be a JSON object no larger than 64 KiB."})),
        )
            .into_response();
    }
    let deadline_seconds = payload
        .get("deadline_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(86400);
    if !(60..=604800).contains(&deadline_seconds)
        || matches!(payload.get("deadline_seconds"), Some(Value::Bool(_)))
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "deadline_seconds must be an integer between 60 and 604800."})),
        )
            .into_response();
    }
    let idempotency_key = header(&headers, "idempotency-key").trim();
    if idempotency_key.is_empty() || idempotency_key.chars().count() > 128 {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "A valid Idempotency-Key header is required."})),
        )
            .into_response();
    }
    let now = chrono::Utc::now();
    let now_text = now.to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let deadline_text = (now + chrono::Duration::seconds(deadline_seconds))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => {
            tracing::error!(%error, "rust workflow run transaction failed");
            return unavailable();
        }
    };
    let version_id = match version_id(&mut transaction, postgres, &workflow).await {
        Ok(Some(id)) => id,
        Ok(None) => {
            let _ = transaction.rollback().await;
            return not_found();
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error_message(error);
        }
    };
    let payload_column = if postgres { "payload::text" } else { "payload" };
    let existing_sql = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(project_id AS TEXT) AS project_id, \
         task_type, {payload_column} AS payload_text FROM core_task \
         WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ?"
    );
    let existing = sqlx::query(&existing_sql)
        .bind(&workflow.tenant_id)
        .bind(idempotency_key)
        .fetch_optional(&mut *transaction)
        .await;
    let existing = match existing {
        Ok(row) => row,
        Err(error) => {
            tracing::error!(%error, "rust workflow run lookup failed");
            return unavailable();
        }
    };
    let (task_id, created) = if let Some(row) = existing {
        let existing_payload = row
            .try_get::<Option<String>, _>("payload_text")
            .ok()
            .flatten()
            .and_then(|text: String| serde_json::from_str::<Value>(text.as_str()).ok());
        let same_project = row.try_get::<String, _>("project_id").ok().as_deref()
            == Some(workflow.project_id.as_str());
        let same_type =
            row.try_get::<String, _>("task_type").ok().as_deref() == Some("workflow.run");
        let expected = json!({
            "workflow_version_id": version_id,
            "input_data": input_data,
            "deadline_seconds": deadline_seconds,
        });
        if !same_project || !same_type || existing_payload.as_ref() != Some(&expected) {
            let _ = transaction.rollback().await;
            return (
                StatusCode::CONFLICT,
                Json(
                    json!({"detail": "Idempotency-Key was already used with a different request."}),
                ),
            )
                .into_response();
        }
        (row.try_get::<String, _>("id").unwrap_or_default(), false)
    } else {
        let task_id = uuid::Uuid::new_v4().to_string();
        let run_id = uuid::Uuid::new_v4().to_string();
        let task_payload = json!({
            "workflow_version_id": version_id,
            "input_data": input_data,
            "deadline_seconds": deadline_seconds,
        });
        let task_insert = if postgres {
            "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, \
             status, idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) \
             VALUES (?::uuid, ?::uuid, ?::uuid, ?, 'workflow.run', ?::jsonb, 'queued', ?, 0, 3, \
             ?::timestamptz, ?::timestamptz, ?::timestamptz)"
        } else {
            "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, \
             status, idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) \
             VALUES (?, ?, ?, ?, 'workflow.run', ?, 'queued', ?, 0, 3, ?, ?, ?)"
        };
        if let Err(error) = sqlx::query(task_insert)
            .bind(&task_id)
            .bind(&workflow.tenant_id)
            .bind(&workflow.project_id)
            .bind(user.id)
            .bind(task_payload.to_string())
            .bind(idempotency_key)
            .bind(&now_text)
            .bind(&now_text)
            .bind(&now_text)
            .execute(&mut *transaction)
            .await
        {
            tracing::error!(%error, "rust workflow task insert failed");
            return unavailable();
        }
        let run_insert = if postgres {
            "INSERT INTO core_workflowrun (id, task_id, workflow_version_id, input_data, state, \
             deadline_at, created_at) VALUES (?::uuid, ?::uuid, ?::uuid, ?::jsonb, ?::jsonb, \
             ?::timestamptz, ?::timestamptz)"
        } else {
            "INSERT INTO core_workflowrun (id, task_id, workflow_version_id, input_data, state, \
             deadline_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        };
        if let Err(error) = sqlx::query(run_insert)
            .bind(&run_id)
            .bind(&task_id)
            .bind(&version_id)
            .bind(input_data.to_string())
            .bind(json!({"input": input_data}).to_string())
            .bind(&deadline_text)
            .bind(&now_text)
            .execute(&mut *transaction)
            .await
        {
            tracing::error!(%error, "rust workflow run insert failed");
            return unavailable();
        }
        if let Err(error) = crate::audit::record_event_in_transaction(
            &mut transaction,
            postgres,
            &crate::audit::AuditEvent {
                tenant_id: Some(&workflow.tenant_id),
                project_id: Some(&workflow.project_id),
                event_type: "workflow.run.created",
                resource_type: "workflow_run",
                resource_id: &run_id,
                outcome: "success",
                actor_id: Some(user.id),
                request_id: header(&headers, "x-request-id"),
                remote_addr: None,
                user_agent: header(&headers, "user-agent"),
                metadata: json!({
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "workflow_version_id": version_id,
                    "deduplicated": false,
                }),
            },
        )
        .await
        {
            return database_error_message(error);
        }
        (task_id, true)
    };
    if let Err(error) = transaction.commit().await {
        tracing::error!(%error, "rust workflow run commit failed");
        return unavailable();
    }
    let sql = "SELECT CAST(id AS TEXT) AS id FROM core_workflowrun WHERE CAST(task_id AS TEXT) = ?";
    let run_id = match sqlx::query_scalar::<_, String>(sql)
        .bind(&task_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(id)) => id,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error.to_string()),
    };
    let run = match fetch_run(pool, &state, &run_id).await {
        Ok(Some(run)) => run,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let status = if created {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };
    (status, Json(run)).into_response()
}

async fn version_id(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    _postgres: bool,
    workflow: &crate::workflows::WorkflowRecord,
) -> Result<Option<String>, String> {
    let sql = "SELECT CAST(id AS TEXT) AS id FROM core_workflowversion \
               WHERE CAST(workflow_id AS TEXT) = ? AND version = ?";
    sqlx::query_scalar::<_, String>(sql)
        .bind(&workflow.id)
        .bind(workflow.current_version)
        .fetch_optional(&mut **transaction)
        .await
        .map_err(|error| error.to_string())
}

async fn fetch_run(
    pool: &AnyPool,
    state: &AppState,
    run_id: &str,
) -> Result<Option<Value>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(r.id AS TEXT) AS id, CAST(r.task_id AS TEXT) AS task_id, \
         CAST(r.workflow_version_id AS TEXT) AS workflow_version_id, t.status AS task_status, \
         t.error{json_cast} AS task_error_text, r.input_data{json_cast} AS input_text, \
         r.state{json_cast} AS state_text, r.output_data{json_cast} AS output_text, \
         {deadline}, {created}, {finished} \
         FROM core_workflowrun r JOIN core_task t ON t.id = r.task_id \
         WHERE CAST(r.id AS TEXT) = ?",
        deadline = timestamp_alias(postgres, "r", "deadline_at", "deadline"),
        created = timestamp_alias(postgres, "r", "created_at", "created"),
        finished = timestamp_alias(postgres, "r", "finished_at", "finished"),
    );
    let row = sqlx::query(&sql)
        .bind(run_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    Ok(Some(json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "task_id": row.try_get::<String, _>("task_id").unwrap_or_default(),
        "workflow_version_id": row.try_get::<String, _>("workflow_version_id").unwrap_or_default(),
        "task_status": row.try_get::<String, _>("task_status").unwrap_or_default(),
        "task_error": json_text(row.try_get::<Option<String>, _>("task_error_text").ok().flatten()),
        "input_data": json_text(row.try_get::<Option<String>, _>("input_text").ok().flatten()),
        "state": json_text(row.try_get::<Option<String>, _>("state_text").ok().flatten()),
        "output_data": json_text(row.try_get::<Option<String>, _>("output_text").ok().flatten()),
        "deadline_at": row.try_get::<Option<String>, _>("deadline").ok().flatten().as_deref().map(normalize_timestamp),
        "steps": Vec::<Value>::new(),
        "created_at": row.try_get::<Option<String>, _>("created").ok().flatten().as_deref().map(normalize_timestamp),
        "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten().as_deref().map(normalize_timestamp),
    })))
}

fn parse_object(body: &axum::body::Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "Request body must be a JSON object."})),
        )
            .into_response()),
    }
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> &'a str {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
}
