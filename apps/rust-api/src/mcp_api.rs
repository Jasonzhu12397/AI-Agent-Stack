#![allow(clippy::result_large_err)]

use std::collections::BTreeSet;

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::Row;

use crate::proxy::AppState;
use crate::tenancy::{project_access, tenant_role};

const PROTOCOL_VERSION: &str = "2025-06-18";
const SUPPORTED_VERSIONS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];
const SUPPORTED_TASK_TYPES: [&str; 2] = ["demo.echo", "demo.sleep"];

fn rpc_error(code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": null})
}

fn rpc_data(code: i64, message: &str, data: Value) -> Value {
    json!({"jsonrpc": "2.0", "error": {"code": code, "message": message, "data": data}, "id": null})
}

fn tools() -> Value {
    json!({"tools": [
        {"name": "platform_list_tasks", "description": "List tasks in this project, newest first.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "status": {"type": "string", "enum": ["queued", "waiting_approval", "running", "cancel_requested", "succeeded", "failed", "cancelled"]}}, "required": [], "additionalProperties": false}, "annotations": {"readOnlyHint": true}},
        {"name": "platform_get_task", "description": "Read one task by id in this project.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"], "additionalProperties": false}, "annotations": {"readOnlyHint": true}},
        {"name": "platform_create_task", "description": "Create a task in this project. Requires editor permission and a unique idempotency key.", "inputSchema": {"type": "object", "properties": {"task_type": {"type": "string", "enum": SUPPORTED_TASK_TYPES}, "payload": {"type": "object"}, "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["task_type", "idempotency_key"], "additionalProperties": false}, "annotations": {"readOnlyHint": false, "destructiveHint": false}},
        {"name": "platform_list_capabilities", "description": "List the connector capabilities installed in this project.", "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false}, "annotations": {"readOnlyHint": true}},
        {"name": "platform_invoke_capability", "description": "Invoke one installed connector capability. High-risk capabilities require platform approval.", "inputSchema": {"type": "object", "properties": {"capability": {"type": "string", "minLength": 1, "maxLength": 64}, "arguments": {"type": "object"}}, "required": ["capability"], "additionalProperties": false}, "annotations": {"readOnlyHint": false}}
    ]})
}

fn text_result(payload: Value) -> Value {
    let encoded = serde_json::to_string(&payload).unwrap_or_else(|_| "null".into());
    if encoded.len() > 65536 {
        return json!({"isError": true, "content": [{"type": "text", "text": "Tool result exceeds 64 KiB."}]});
    }
    json!({"content": [{"type": "text", "text": encoded}], "structuredContent": payload, "isError": false})
}

async fn list_tasks(
    state: &AppState,
    tenant: &str,
    project: &str,
    args: &Value,
) -> Result<Value, Value> {
    let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(20);
    if !(1..=50).contains(&limit) {
        return Err(rpc_error(
            -32602,
            "limit must be an integer between 1 and 50.",
        ));
    }
    let status = args.get("status").and_then(Value::as_str);
    if let Some(status) = status {
        if ![
            "queued",
            "waiting_approval",
            "running",
            "cancel_requested",
            "succeeded",
            "failed",
            "cancelled",
        ]
        .contains(&status)
        {
            return Err(rpc_error(-32602, "status is not a known task status."));
        }
    }
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    let mut tasks = crate::tasks::task_rows(pool, state, Some(tenant), Some(project), 0, limit)
        .await
        .map_err(|_| rpc_error(-32603, "Internal error."))?;
    if let Some(status) = status {
        tasks.retain(|task| task.get("status").and_then(Value::as_str) == Some(status));
    }
    Ok(json!({"tasks": tasks, "limit": limit}))
}

async fn get_task(
    state: &AppState,
    tenant: &str,
    project: &str,
    args: &Value,
) -> Result<Value, Value> {
    let Some(task_id) = args
        .get("task_id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
    else {
        return Err(rpc_error(-32602, "task_id is required."));
    };
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    let task = crate::tasks::task_json(pool, state, task_id)
        .await
        .map_err(|_| rpc_error(-32603, "Internal error."))?;
    match task {
        Some(task)
            if task.get("tenant_id").and_then(Value::as_str) == Some(tenant)
                && task.get("project_id").and_then(Value::as_str) == Some(project) =>
        {
            Ok(json!({"task": task}))
        }
        _ => Err(rpc_error(-32602, "Task not found in this project.")),
    }
}

async fn create_task(
    state: &AppState,
    tenant: &str,
    project: &str,
    user_id: i64,
    args: &Value,
) -> Result<Value, Value> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    let role = tenant_role(pool, tenant, user_id)
        .await
        .unwrap_or(None)
        .unwrap_or_default();
    if role != "owner"
        && role != "admin"
        && !crate::tasks::project_editor(pool, project, user_id)
            .await
            .unwrap_or(false)
    {
        return Err(rpc_error(-32602, "Project editor permission required."));
    }
    let task_type = args.get("task_type").and_then(Value::as_str).unwrap_or("");
    if !SUPPORTED_TASK_TYPES.contains(&task_type) {
        return Err(rpc_error(-32602, "Unsupported task_type."));
    }
    let payload = args.get("payload").cloned().unwrap_or_else(|| json!({}));
    if !payload.is_object()
        || serde_json::to_string(&payload)
            .map(|text| text.len())
            .unwrap_or(0)
            > 65536
    {
        return Err(rpc_error(
            -32602,
            "payload must be a JSON object no larger than 64 KiB.",
        ));
    }
    let key = args
        .get("idempotency_key")
        .and_then(Value::as_str)
        .unwrap_or("");
    if key.is_empty() || key.len() > 128 {
        return Err(rpc_error(
            -32602,
            "idempotency_key is required and must be at most 128 chars.",
        ));
    }
    let existing: Option<String> = sqlx::query_scalar("SELECT CAST(id AS TEXT) FROM core_task WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1")
        .bind(tenant).bind(key).fetch_optional(pool).await.map_err(|_| rpc_error(-32603, "Internal error."))?;
    let created = existing.is_none();
    let id = if let Some(id) = existing {
        id
    } else {
        let id = uuid::Uuid::new_v4().to_string();
        let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
        sqlx::query("INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, status, attempts, max_attempts, available_at, created_at, updated_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 3, ?, ?, ?, ?)")
            .bind(&id).bind(tenant).bind(project).bind(user_id).bind(task_type).bind(payload.to_string()).bind(&now).bind(&now).bind(&now).bind(key)
            .execute(pool).await.map_err(|_| rpc_error(-32603, "Internal error."))?;
        id
    };
    let task = crate::tasks::task_json(pool, state, &id)
        .await
        .map_err(|_| rpc_error(-32603, "Internal error."))?
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    if !created
        && (task.get("project_id").and_then(Value::as_str) != Some(project)
            || task.get("task_type").and_then(Value::as_str) != Some(task_type)
            || task.get("payload") != Some(&payload))
    {
        return Err(rpc_error(
            -32602,
            "idempotency_key was already used with a different request.",
        ));
    }
    if created {
        let _ = crate::audit::record_event(
            pool,
            state.config.database_url.as_deref().unwrap_or_default(),
            crate::audit::AuditEvent {
                tenant_id: Some(tenant),
                project_id: Some(project),
                event_type: "task.created",
                resource_type: "task",
                resource_id: &id,
                outcome: "success",
                actor_id: Some(user_id),
                request_id: "",
                remote_addr: None,
                user_agent: "",
                metadata: json!({"source": "mcp_server"}),
            },
        )
        .await;
    }
    Ok(json!({"task": task, "created": created}))
}

async fn list_capabilities(state: &AppState, tenant: &str, project: &str) -> Result<Value, Value> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    let rows = sqlx::query(
        "SELECT CAST(installation.id AS TEXT) AS installation_id, installation.plugin_version, CAST(installation.granted_capabilities AS TEXT) AS granted, \
         plugin.plugin_key, CAST(plugin.capabilities AS TEXT) AS capabilities, CAST(plugin.manifest AS TEXT) AS manifest, \
         CAST(plugin.input_schema AS TEXT) AS input_schema, CAST(plugin.output_schema AS TEXT) AS output_schema, plugin.timeout_seconds \
         FROM core_agentplugininstallation installation JOIN core_agentplugindefinition plugin ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.tenant_id AS TEXT) = ? AND CAST(installation.project_id AS TEXT) = ? AND installation.status = 'installed' AND plugin.enabled = 1",
    ).bind(tenant).bind(project).fetch_all(pool).await.map_err(|_| rpc_error(-32603, "Internal error."))?;
    let mut capabilities = Vec::new();
    for row in rows {
        let definitions = crate::plugin_api::capability_definitions(
            row.try_get("capabilities").ok(),
            row.try_get("manifest").ok(),
            row.try_get("input_schema").ok(),
            row.try_get("output_schema").ok(),
            row.try_get("timeout_seconds").unwrap_or(30),
        );
        for definition in definitions {
            let name = definition.get("name").and_then(Value::as_str).unwrap_or("");
            if name.is_empty() {
                continue;
            }
            capabilities.push(json!({
                "capability": name,
                "plugin_key": row.try_get::<String, _>("plugin_key").unwrap_or_default(),
                "plugin_version": row.try_get::<String, _>("plugin_version").unwrap_or_default(),
                "risk_level": definition.get("risk_level").and_then(Value::as_str).unwrap_or("low"),
                "input_schema": definition.get("input_schema").cloned().unwrap_or(json!({})),
            }));
        }
    }
    capabilities.sort_by(|left, right| {
        left["capability"]
            .as_str()
            .unwrap_or("")
            .cmp(right["capability"].as_str().unwrap_or(""))
            .then(
                left["plugin_key"]
                    .as_str()
                    .unwrap_or("")
                    .cmp(right["plugin_key"].as_str().unwrap_or("")),
            )
    });
    Ok(json!({"capabilities": capabilities}))
}

pub(crate) async fn invoke(
    state: &AppState,
    tenant: &str,
    project: &str,
    args: &Value,
    plugin_id: Option<&str>,
) -> Result<Value, Value> {
    let capability = args.get("capability").and_then(Value::as_str).unwrap_or("");
    if capability.is_empty() || capability.len() > 64 {
        return Err(rpc_error(-32602, "capability is required."));
    }
    let invocation_args = args.get("arguments").cloned().unwrap_or_else(|| json!({}));
    if !invocation_args.is_object() {
        return Err(rpc_error(-32602, "arguments must be an object."));
    }
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| rpc_error(-32603, "Internal error."))?;
    let rows = sqlx::query(
        "SELECT CAST(installation.id AS TEXT) AS installation_id, CAST(installation.granted_capabilities AS TEXT) AS granted, installation.status, installation.credential_ref, \
         plugin.plugin_key, plugin.protocol, plugin.endpoint_url, plugin.auth_required, CAST(plugin.capabilities AS TEXT) AS capabilities, CAST(plugin.manifest AS TEXT) AS manifest, \
         CAST(plugin.input_schema AS TEXT) AS input_schema, CAST(plugin.output_schema AS TEXT) AS output_schema, plugin.timeout_seconds, plugin.enabled \
         FROM core_agentplugininstallation installation JOIN core_agentplugindefinition plugin ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.tenant_id AS TEXT) = ? AND CAST(installation.project_id AS TEXT) = ? AND (? IS NULL OR CAST(plugin.id AS TEXT) = ?)",
    ).bind(tenant).bind(project).bind(plugin_id).bind(plugin_id).fetch_all(pool).await.map_err(|_| rpc_error(-32603, "Internal error."))?;
    let mut selected = None;
    for row in rows {
        let definitions = crate::plugin_api::capability_definitions(
            row.try_get("capabilities").ok(),
            row.try_get("manifest").ok(),
            row.try_get("input_schema").ok(),
            row.try_get("output_schema").ok(),
            row.try_get("timeout_seconds").unwrap_or(30),
        );
        if let Some(definition) = definitions
            .iter()
            .find(|definition| definition.get("name").and_then(Value::as_str) == Some(capability))
        {
            selected = Some((row, definition.clone()));
            break;
        }
    }
    let Some((row, definition)) = selected else {
        return Err(rpc_data(
            -32602,
            "The capability is not installed in this project.",
            json!({"code": "CAPABILITY_NOT_DECLARED"}),
        ));
    };
    if row.try_get::<String, _>("status").unwrap_or_default() != "installed"
        || row.try_get::<i64, _>("enabled").unwrap_or(0) == 0
    {
        return Err(rpc_data(
            -32602,
            "The agent plugin installation is disabled.",
            json!({"code": "CAPABILITY_DISABLED"}),
        ));
    }
    let granted: Vec<String> = row
        .try_get::<Option<String>, _>("granted")
        .ok()
        .flatten()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_array().cloned())
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let granted_set: BTreeSet<String> = granted.into_iter().collect();
    if !granted_set.contains(capability) {
        return Err(rpc_data(
            -32602,
            "The capability is not granted to this installation.",
            json!({"code": "CAPABILITY_NOT_INSTALLED"}),
        ));
    }
    let risk = definition
        .get("risk_level")
        .and_then(Value::as_str)
        .unwrap_or("low");
    if matches!(risk, "high" | "critical" | "unclassified") {
        return Err(rpc_data(
            -32602,
            "This capability requires explicit approval before execution.",
            json!({"code": "APPROVAL_REQUIRED"}),
        ));
    }
    if row.try_get::<i64, _>("auth_required").unwrap_or(0) != 0
        && row
            .try_get::<String, _>("credential_ref")
            .unwrap_or_default()
            .is_empty()
    {
        return Err(rpc_data(
            -32602,
            "A credential reference is required for this agent plugin.",
            json!({"code": "CAPABILITY_AUTH_REQUIRED"}),
        ));
    }
    let protocol: String = row.try_get("protocol").unwrap_or_default();
    let installation_id: String = row.try_get("installation_id").unwrap_or_default();
    if protocol == "open_data" {
        // Built-in map and weather capabilities run in this process; there is
        // no endpoint or credential to reach.
        let started = std::time::Instant::now();
        let outcome = crate::open_data::invoke(capability, &invocation_args).await;
        let latency = started.elapsed().as_millis() as i64;
        let (status, code) = match &outcome {
            Ok(_) => ("succeeded", ""),
            Err(error) => ("failed", error.code),
        };
        let _ = sqlx::query(
            "INSERT INTO core_agentcapabilityinvocation (id, installation_id, capability, protocol, status, error_code, latency_ms, created_at) \
             VALUES (?, ?, ?, 'open_data', ?, ?, ?, CURRENT_TIMESTAMP)",
        )
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(&installation_id)
        .bind(capability)
        .bind(status)
        .bind(code)
        .bind(latency)
        .execute(pool)
        .await;
        return match outcome {
            Ok(result) => Ok(result),
            Err(error) => Err(rpc_data(
                -32602,
                &error.message,
                json!({"code": error.code, "status": error.status}),
            )),
        };
    }
    if protocol != "http_json" {
        return Err(rpc_data(
            -32602,
            "Protocol has no invocation adapter.",
            json!({"code": "CAPABILITY_PROTOCOL_UNSUPPORTED"}),
        ));
    }
    let endpoint = row.try_get::<String, _>("endpoint_url").unwrap_or_default();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|_| rpc_error(-32603, "Internal error."))?;
    let started = std::time::Instant::now();
    let response = client.post(&endpoint).json(&json!({"capability": capability, "arguments": invocation_args, "plugin_key": row.try_get::<String, _>("plugin_key").unwrap_or_default()})).send().await;
    let latency = started.elapsed().as_millis() as i64;
    let response = match response {
        Ok(response) if response.status().is_success() => response,
        _ => {
            let _ = sqlx::query("INSERT INTO core_agentcapabilityinvocation (id, installation_id, capability, protocol, status, error_code, latency_ms, created_at) VALUES (?, ?, ?, 'http_json', 'failed', 'CAPABILITY_UPSTREAM_ERROR', ?, CURRENT_TIMESTAMP)")
                .bind(uuid::Uuid::new_v4().to_string()).bind(&installation_id).bind(capability).bind(latency).execute(pool).await;
            return Err(rpc_data(
                -32602,
                "Capability invocation failed.",
                json!({"code": "CAPABILITY_UPSTREAM_ERROR"}),
            ));
        }
    };
    let mut result: Value = response.json().await.unwrap_or_else(|_| json!({}));
    // Studio plugins declare a single free-form ``text`` output: the platform
    // handed the model the JSON body as text so the schema stayed honest.
    let studio = row
        .try_get::<String, _>("manifest")
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|manifest| manifest.get("studio").cloned())
        .map(|studio| !studio.is_null())
        .unwrap_or(false);
    let text_only = definition
        .get("output_schema")
        .and_then(|schema| schema.get("properties"))
        .and_then(Value::as_object)
        .map(|properties| properties.len() == 1 && properties.contains_key("text"))
        .unwrap_or(false);
    if studio && text_only {
        let text: String = serde_json::to_string(&result).unwrap_or_default();
        result = json!({"text": text.chars().take(65_536).collect::<String>()});
    }
    let _ = sqlx::query("INSERT INTO core_agentcapabilityinvocation (id, installation_id, capability, protocol, status, error_code, latency_ms, created_at) VALUES (?, ?, ?, 'http_json', 'succeeded', '', ?, CURRENT_TIMESTAMP)")
        .bind(uuid::Uuid::new_v4().to_string()).bind(&installation_id).bind(capability).bind(latency).execute(pool).await;
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or_default(),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant),
            project_id: Some(project),
            event_type: "mcp_server.capability_invoked",
            resource_type: "agent_plugin",
            resource_id: capability,
            outcome: "success",
            actor_id: None,
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"source": "mcp_server"}),
        },
    )
    .await;
    Ok(json!({"capability": capability, "result": result}))
}

async fn dispatch(
    state: &AppState,
    tenant: &str,
    project: &str,
    user_id: i64,
    message: &Value,
) -> Result<Option<Value>, Value> {
    if !message.is_object() || message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err(rpc_error(
            -32600,
            "Request must be a JSON object with jsonrpc '2.0'.",
        ));
    }
    let Some(method) = message
        .get("method")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return Err(rpc_error(-32600, "method is required."));
    };
    let params = match message.get("params") {
        None | Some(Value::Null) => json!({}),
        Some(Value::Object(map)) => Value::Object(map.clone()),
        Some(_) => return Err(rpc_error(-32602, "params must be an object.")),
    };
    match method {
        "initialize" => {
            let requested = params
                .get("protocolVersion")
                .and_then(Value::as_str)
                .unwrap_or(PROTOCOL_VERSION);
            let version = if SUPPORTED_VERSIONS.contains(&requested) {
                requested
            } else {
                PROTOCOL_VERSION
            };
            Ok(Some(
                json!({"protocolVersion": version, "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "ai-agent-stack", "version": "1.0.0"}}),
            ))
        }
        "notifications/initialized" => Ok(None),
        "ping" => Ok(Some(json!({}))),
        "tools/list" => Ok(Some(tools())),
        "tools/call" => {
            let name = params.get("name").and_then(Value::as_str).unwrap_or("");
            let arguments = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            if !arguments.is_object() {
                return Err(rpc_error(-32602, "Tool arguments must be an object."));
            }
            let result = match name {
                "platform_list_tasks" => list_tasks(state, tenant, project, &arguments).await?,
                "platform_get_task" => get_task(state, tenant, project, &arguments).await?,
                "platform_create_task" => {
                    create_task(state, tenant, project, user_id, &arguments).await?
                }
                "platform_list_capabilities" => list_capabilities(state, tenant, project).await?,
                "platform_invoke_capability" => {
                    invoke(state, tenant, project, &arguments, None).await?
                }
                _ => return Err(rpc_error(-32602, "Unknown tool.")),
            };
            Ok(Some(text_result(result)))
        }
        _ => Err(rpc_error(-32601, "Method not found.")),
    }
}

/// Native replacement for `POST /api/v1/tenants/{t}/projects/{p}/mcp/`.
pub async fn serve(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(rpc_error(-32603, "Internal error.")),
        )
            .into_response();
    };
    if !project_access(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "Project not found."})),
        )
            .into_response();
    }
    let _role = tenant_role(pool, &tenant_id, user.id).await.unwrap_or(None);
    if body.is_empty() || body.len() > 1024 * 1024 {
        return Json(rpc_error(
            -32600,
            "An empty body is not a JSON-RPC message.",
        ))
        .into_response();
    }
    let payload: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(_) => return Json(rpc_error(-32700, "Invalid JSON.")).into_response(),
    };
    if payload.is_array() {
        return Json(rpc_error(-32600, "Batch requests are not supported.")).into_response();
    }
    let id = payload.get("id").cloned().unwrap_or(Value::Null);
    match dispatch(&state, &tenant_id, &project_id, user.id, &payload).await {
        Ok(None) => StatusCode::ACCEPTED.into_response(),
        Ok(Some(result)) => {
            Json(json!({"jsonrpc": "2.0", "id": id, "result": result})).into_response()
        }
        Err(mut error) => {
            error["id"] = id;
            Json(error).into_response()
        }
    }
}
