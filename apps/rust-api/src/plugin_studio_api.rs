#![allow(clippy::result_large_err)]
//! Native Plugin Studio discovery, publish and connection test endpoints.

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine as _;
use serde_json::{json, Map, Value};
use sqlx::Row;

use crate::billing_api::{require_user, tenant_admin, tenant_member};
use crate::proxy::AppState;

const MAX_TOOLS: usize = 32;

fn studio_error(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn database_error(message: String) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"code": "DATABASE_UNAVAILABLE", "message": message}})),
    )
        .into_response()
}

fn safe_name(raw: &str) -> Option<String> {
    let mut cleaned = String::new();
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() {
            cleaned.push(ch.to_ascii_lowercase());
        } else if matches!(ch, '_' | '-') {
            cleaned.push('_');
        }
    }
    let trimmed = cleaned.trim_matches('_');
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.chars().take(64).collect())
    }
}

fn text_output_schema() -> Value {
    json!({"type": "object", "properties": {"text": {"type": "string", "maxLength": 65536}}, "required": ["text"], "additionalProperties": false})
}

fn parameter_schema(raw: &Value, location: &str) -> Value {
    let mut schema = raw
        .get("schema")
        .cloned()
        .unwrap_or_else(|| json!({"type": "string"}));
    let kind = schema
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("string");
    if matches!(location, "path" | "query") && !matches!(kind, "string" | "integer") {
        schema = json!({"type": "string", "maxLength": 65536});
    }
    schema
}

struct Discovered {
    protocol: &'static str,
    endpoint_url: String,
    manifest: Value,
    capabilities: Vec<Value>,
    skipped: Vec<Value>,
    connection: Value,
}

fn build_openapi_manifest(
    base_url: &str,
    document_url: &str,
    document: &Value,
) -> Result<Discovered, Response> {
    let paths = document
        .get("paths")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            studio_error(
                StatusCode::BAD_REQUEST,
                "PLUGIN_DOCUMENT_INVALID",
                "The document is not an OpenAPI document with paths.",
            )
        })?;
    if document.get("openapi").is_none() && document.get("swagger").is_none() {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_DOCUMENT_INVALID",
            "The document is not an OpenAPI document.",
        ));
    }
    let mut capabilities = Vec::new();
    let mut operations = Map::new();
    let mut skipped = Vec::new();
    let mut names = std::collections::BTreeSet::new();
    for (raw_path, path_item) in paths {
        if !raw_path.starts_with('/') {
            continue;
        }
        let Some(path_item) = path_item.as_object() else {
            continue;
        };
        for (method, operation) in path_item {
            let method = method.to_ascii_lowercase();
            if !matches!(method.as_str(), "get" | "post") {
                if matches!(
                    method.as_str(),
                    "put" | "patch" | "delete" | "head" | "options" | "trace"
                ) {
                    skipped.push(json!({"method": method.to_uppercase(), "path": raw_path, "reason": "Method is not executable by the safe HTTP connector."}));
                }
                continue;
            }
            let Some(operation) = operation.as_object() else {
                continue;
            };
            if capabilities.len() >= MAX_TOOLS {
                skipped.push(json!({"method": method.to_uppercase(), "path": raw_path, "reason": "The 32 capability limit was reached."}));
                continue;
            }
            let default_name = format!("{method}_{raw_path}");
            let raw_name = operation
                .get("operationId")
                .and_then(Value::as_str)
                .unwrap_or(&default_name);
            let base_name = safe_name(raw_name)
                .unwrap_or_else(|| safe_name(&default_name).unwrap_or_else(|| "operation".into()));
            let mut name = base_name.clone();
            let mut suffix = 2;
            while names.contains(&name) {
                name = format!("{base_name}_{suffix}");
                suffix += 1;
            }
            names.insert(name.clone());
            let mut properties = Map::new();
            let mut required = Vec::new();
            let mut path_params = Map::new();
            let mut query_params = Map::new();
            for source in [path_item.get("parameters"), operation.get("parameters")]
                .into_iter()
                .flatten()
            {
                let Some(list) = source.as_array() else {
                    continue;
                };
                for parameter in list {
                    let Some(parameter) = parameter.as_object() else {
                        continue;
                    };
                    let Some(field) = parameter.get("name").and_then(Value::as_str) else {
                        continue;
                    };
                    let location = parameter.get("in").and_then(Value::as_str).unwrap_or("");
                    if !matches!(location, "path" | "query") {
                        continue;
                    }
                    let Some(argument) = safe_name(field) else {
                        continue;
                    };
                    if properties.contains_key(&argument) {
                        continue;
                    }
                    properties.insert(
                        argument.clone(),
                        parameter_schema(&Value::Object(parameter.clone()), location),
                    );
                    if parameter
                        .get("required")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                        || location == "path"
                    {
                        required.push(Value::String(argument.clone()));
                    }
                    if location == "path" {
                        path_params.insert(argument.clone(), Value::String(argument));
                    } else {
                        query_params.insert(field.to_string(), Value::String(argument));
                    }
                }
            }
            let body_schema = operation
                .get("requestBody")
                .and_then(|body| body.get("content"))
                .and_then(Value::as_object)
                .and_then(|content| {
                    content
                        .iter()
                        .find(|(media, _)| *media == "application/json" || media.ends_with("+json"))
                        .map(|(_, media)| media)
                })
                .and_then(|media| media.get("schema"))
                .cloned();
            let body_required = operation
                .get("requestBody")
                .and_then(|body| body.get("required"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if let Some(schema) = body_schema {
                let normalized = if schema.get("type").and_then(Value::as_str) == Some("object") {
                    schema
                } else {
                    json!({"type": "object", "properties": {"value": schema}, "required": ["value"], "additionalProperties": false})
                };
                properties.insert("body".into(), normalized);
                if body_required {
                    required.push(Value::String("body".into()));
                }
            }
            let input_schema = json!({
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": false,
            });
            let read_only = method == "get";
            capabilities.push(json!({
                "name": name,
                "description": operation.get("summary").or_else(|| operation.get("description")).and_then(Value::as_str).unwrap_or("").chars().take(1000).collect::<String>(),
                "input_schema": input_schema,
                "output_schema": text_output_schema(),
                "risk_level": if read_only { "low" } else { "high" },
                "timeout_seconds": 30,
                "source": "declared",
                "remote_operation": format!("{} {raw_path}", method.to_uppercase()),
            }));
            let mut entry = Map::new();
            entry.insert("method".into(), Value::String(method.to_uppercase()));
            entry.insert("path".into(), Value::String(raw_path.clone()));
            entry.insert("path_params".into(), Value::Object(path_params));
            entry.insert("query_params".into(), Value::Object(query_params));
            entry.insert("body_param".into(), Value::String("body".into()));
            entry.insert("read_only".into(), Value::Bool(read_only));
            if !read_only {
                entry.insert("requires_approval".into(), Value::Bool(true));
            }
            operations.insert(name, Value::Object(entry));
        }
    }
    if capabilities.is_empty() {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_NO_CAPABILITIES",
            "No safe GET or POST operations were found in the OpenAPI document.",
        ));
    }
    let approval: Vec<Value> = capabilities
        .iter()
        .filter(|entry| entry.get("risk_level").and_then(Value::as_str) == Some("high"))
        .filter_map(|entry| entry.get("name").cloned())
        .collect();
    let manifest = json!({
        "version": 1,
        "capabilities": capabilities,
        "http": {"base_url": base_url, "operations": operations},
        "permission_policy": {
            "default": "read_only",
            "approval_required": approval,
            "blocked_methods": ["DELETE", "HEAD", "OPTIONS", "PATCH", "PUT", "TRACE"],
            "max_risk": "high",
        },
        "studio": {
            "plugin_type": "openapi",
            "document_url": document_url,
            "document_status": 200,
            "auth_mode": "none",
            "discovered_at": chrono::Utc::now().to_rfc3339(),
        },
    });
    Ok(Discovered {
        protocol: "http_json",
        endpoint_url: base_url.to_string(),
        manifest,
        capabilities,
        skipped,
        connection: json!({
            "status": "success",
            "http_status": 200,
            "authenticated": false,
            "message": "OpenAPI document fetched and validated.",
        }),
    })
}

async fn fetch_document(url: &str, document: Option<&Value>) -> Result<(Value, String), Response> {
    if let Some(document) = document {
        if !document.is_object() {
            return Err(studio_error(
                StatusCode::BAD_REQUEST,
                "PLUGIN_DOCUMENT_INVALID",
                "The pasted OpenAPI document must be an object.",
            ));
        }
        return Ok((document.clone(), "inline://openapi.json".into()));
    }
    if let Some(url) = url.trim().strip_prefix("inline://") {
        let _ = url;
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_DOCUMENT_REQUIRED",
            "An inline document must be supplied.",
        ));
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|error| database_error(error.to_string()))?;
    let response = client.get(url).send().await.map_err(|error| {
        studio_error(
            StatusCode::BAD_GATEWAY,
            "PLUGIN_DISCOVERY_FAILED",
            &format!("The document could not be fetched: {error}"),
        )
    })?;
    if !response.status().is_success() {
        return Err(studio_error(
            StatusCode::BAD_GATEWAY,
            "PLUGIN_DISCOVERY_FAILED",
            &format!("The document endpoint returned HTTP {}.", response.status()),
        ));
    }
    let text = response.text().await.map_err(|error| {
        studio_error(
            StatusCode::BAD_GATEWAY,
            "PLUGIN_DISCOVERY_FAILED",
            &format!("The document body could not be read: {error}"),
        )
    })?;
    let document: Value = serde_json::from_str(&text).map_err(|error| {
        studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_DOCUMENT_INVALID",
            &format!("The document is not valid JSON: {error}"),
        )
    })?;
    Ok((document, url.to_string()))
}

async fn discover_mcp(endpoint_url: &str) -> Result<Discovered, Response> {
    use sha2::Digest;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|error| database_error(error.to_string()))?;
    let initialize = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ai-agent-stack", "version": "1.0"},
        },
    });
    let response = client
        .post(endpoint_url)
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .json(&initialize)
        .send()
        .await
        .map_err(|error| {
            studio_error(
                StatusCode::BAD_GATEWAY,
                "PLUGIN_DISCOVERY_FAILED",
                &format!("The MCP endpoint could not be reached: {error}"),
            )
        })?;
    if !response.status().is_success() {
        return Err(studio_error(
            StatusCode::BAD_GATEWAY,
            "PLUGIN_AUTH_REJECTED",
            &format!("The MCP endpoint returned HTTP {}.", response.status()),
        ));
    }
    let list = json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}});
    let response = client
        .post(endpoint_url)
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .json(&list)
        .send()
        .await
        .map_err(|error| {
            studio_error(
                StatusCode::BAD_GATEWAY,
                "PLUGIN_DISCOVERY_FAILED",
                &format!("The MCP tools/list call failed: {error}"),
            )
        })?;
    let body: Value = response.json().await.map_err(|error| {
        studio_error(
            StatusCode::BAD_GATEWAY,
            "PLUGIN_DISCOVERY_FAILED",
            &format!("The MCP response was not valid JSON: {error}"),
        )
    })?;
    let tools = body
        .pointer("/result/tools")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            studio_error(
                StatusCode::BAD_GATEWAY,
                "PLUGIN_DISCOVERY_FAILED",
                "The MCP server returned no tools list.",
            )
        })?;
    let mut capabilities = Vec::new();
    let mut names = std::collections::BTreeSet::new();
    for tool in tools.iter().take(MAX_TOOLS) {
        let Some(raw_name) = tool.get("name").and_then(Value::as_str) else {
            continue;
        };
        let Some(mut name) = safe_name(raw_name) else {
            continue;
        };
        let base = name.clone();
        let mut suffix = 2;
        while names.contains(&name) {
            name = format!("{base}_{suffix}");
            suffix += 1;
        }
        names.insert(name.clone());
        let input_schema = tool
            .get("inputSchema")
            .or_else(|| tool.get("input_schema"))
            .cloned()
            .unwrap_or_else(
                || json!({"type": "object", "properties": {}, "additionalProperties": false}),
            );
        capabilities.push(json!({
            "name": name,
            "description": tool.get("description").and_then(Value::as_str).unwrap_or("").chars().take(1000).collect::<String>(),
            "input_schema": input_schema,
            "output_schema": text_output_schema(),
            "risk_level": "unclassified",
            "review_required": true,
            "timeout_seconds": 30,
            "source": "discovered",
            "remote_name": raw_name,
        }));
    }
    if capabilities.is_empty() {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_NO_CAPABILITIES",
            "The MCP server exposed no usable tools.",
        ));
    }
    let names: Vec<String> = capabilities
        .iter()
        .filter_map(|entry| {
            entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    let manifest = json!({
        "version": 1,
        "capabilities": capabilities,
        "mcp": {
            "timeout_seconds": 30,
            "discovery": {
                "endpoint": format!("{:x}", sha2::Sha256::digest(endpoint_url.as_bytes())),
                "discovered_at": chrono::Utc::now().to_rfc3339(),
                "tool_count": names.len(),
                "tools": names,
            },
        },
        "permission_policy": {
            "default": "deny",
            "approval_required": capabilities.iter().filter_map(|entry| entry.get("name").cloned()).collect::<Vec<_>>(),
            "blocked_methods": [],
            "max_risk": "unclassified",
            "review_required": true,
        },
        "studio": {
            "plugin_type": "mcp",
            "auth_mode": "none",
            "discovered_at": chrono::Utc::now().to_rfc3339(),
        },
    });
    let count = capabilities.len();
    Ok(Discovered {
        protocol: "mcp",
        endpoint_url: endpoint_url.to_string(),
        manifest,
        capabilities,
        skipped: Vec::new(),
        connection: json!({
            "status": "success",
            "http_status": 200,
            "authenticated": false,
            "message": format!("MCP initialized and listed {count} tools."),
        }),
    })
}

async fn discover(payload: &Value) -> Result<Discovered, Response> {
    let plugin_type = payload
        .get("plugin_type")
        .and_then(Value::as_str)
        .unwrap_or("openapi");
    let base_url = payload
        .get("base_url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if base_url.is_empty() {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "base_url is required.",
        ));
    }
    if !(base_url.starts_with("http://") || base_url.starts_with("https://")) {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "base_url must be an absolute HTTP(S) URL.",
        ));
    }
    if plugin_type == "mcp" {
        return discover_mcp(base_url).await;
    }
    if plugin_type != "openapi" {
        return Err(studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_TYPE_UNSUPPORTED",
            "Unsupported plugin type.",
        ));
    }
    let pasted = payload.get("document").filter(|value| !value.is_null());
    let document_url = payload
        .get("document_url")
        .and_then(Value::as_str)
        .unwrap_or("");
    let (document, actual_url) = if let Some(pasted) = pasted {
        fetch_document("", Some(pasted)).await?
    } else if !document_url.trim().is_empty() {
        fetch_document(document_url, None).await?
    } else {
        let mut last_error = None;
        for candidate in [
            format!("{}/openapi.json", base_url.trim_end_matches('/')),
            format!("{}/swagger.json", base_url.trim_end_matches('/')),
            format!("{}/docs/openapi.json", base_url.trim_end_matches('/')),
        ] {
            match fetch_document(&candidate, None).await {
                Ok(result) => return build_openapi_manifest(base_url, &result.1, &result.0),
                Err(error) => last_error = Some(error),
            }
        }
        return Err(last_error.unwrap_or_else(|| {
            studio_error(
                StatusCode::BAD_REQUEST,
                "PLUGIN_DOCUMENT_REQUIRED",
                "No OpenAPI document was supplied and common discovery paths were not found.",
            )
        }));
    };
    build_openapi_manifest(base_url, &actual_url, &document)
}

fn fingerprint(manifest: &Value) -> String {
    use sha2::Digest;
    let stable = manifest
        .as_object()
        .map(|map| {
            let mut clone = map.clone();
            clone.remove("studio");
            Value::Object(clone)
        })
        .unwrap_or_else(|| manifest.clone());
    let encoded = serde_json::to_string(&stable).unwrap_or_default();
    format!("{:x}", sha2::Sha256::digest(encoded.as_bytes()))
}

fn discover_response(discovered: Discovered) -> Response {
    let schema_fingerprint = fingerprint(&discovered.manifest);
    Json(json!({
        "protocol": discovered.protocol,
        "endpoint_url": discovered.endpoint_url,
        "manifest": discovered.manifest,
        "capabilities": discovered.capabilities,
        "skipped": discovered.skipped,
        "connection": discovered.connection,
        "schema_fingerprint": schema_fingerprint,
        "auth_mode": "none",
        "credential_configured": false,
        "credential_kind": "none",
        "generated": {
            "manifest": true,
            "input_schema": true,
            "output_schema": true,
            "auth_schema": true,
            "tool_definitions": discovered.capabilities.len(),
            "dynamic_form": true,
            "permission_policy": true,
        },
    }))
    .into_response()
}

async fn authorize(
    state: &AppState,
    headers: &HeaderMap,
    tenant_id: &str,
    admin_only: bool,
) -> Result<i64, Response> {
    let user = require_user(state, headers).await?;
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(database_error("Identity pool is not configured.".into()));
    };
    if !tenant_member(pool, tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return Err(studio_error(
            StatusCode::NOT_FOUND,
            "NOT_FOUND",
            "Not found.",
        ));
    }
    if admin_only {
        match tenant_admin(pool, tenant_id, user.id).await {
            Ok(true) => {}
            Ok(false) => {
                return Err(studio_error(
                    StatusCode::FORBIDDEN,
                    "TENANT_ADMIN_REQUIRED",
                    "Tenant administrator permission required.",
                ))
            }
            Err(error) => return Err(database_error(error)),
        }
    }
    Ok(user.id)
}

pub async fn studio_discover(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    if let Err(response) = authorize(&state, &headers, &tenant_id, true).await {
        return response;
    }
    match discover(&payload).await {
        Ok(discovered) => discover_response(discovered),
        Err(response) => response,
    }
}

pub async fn studio_test(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    if let Err(response) = authorize(&state, &headers, &tenant_id, true).await {
        return response;
    }
    match discover(&payload).await {
        Ok(discovered) => Json(json!({
            "status": "success",
            "connection": discovered.connection,
            "tool_count": discovered.capabilities.len(),
            "protocol": discovered.protocol,
        }))
        .into_response(),
        Err(response) => response,
    }
}

fn valid_plugin_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
        && !value.starts_with('-')
        && !value.ends_with('-')
}

fn next_version(previous: &str) -> String {
    let parts: Vec<&str> = previous.split('.').collect();
    if parts.len() == 3 {
        if let (Ok(major), Ok(minor), Ok(patch)) = (
            parts[0].parse::<u64>(),
            parts[1].parse::<u64>(),
            parts[2].parse::<u64>(),
        ) {
            return format!("{major}.{minor}.{}", patch + 1);
        }
    }
    if previous.is_empty() {
        "1.0.0".into()
    } else {
        format!("{previous}.1")
    }
}

pub async fn studio_publish(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let user_id = match authorize(&state, &headers, &tenant_id, true).await {
        Ok(id) => id,
        Err(response) => return response,
    };
    let plugin_key = payload
        .get("plugin_key")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !valid_plugin_key(plugin_key) {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_KEY_INVALID",
            "plugin_key must be a lowercase hyphenated identifier.",
        );
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if name.is_empty() || name.chars().count() > 120 {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_NAME_INVALID",
            "name is required and must be at most 120 characters.",
        );
    }
    let description = payload
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    if description.chars().count() > 4000 {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_DESCRIPTION_INVALID",
            "description must be at most 4,000 characters.",
        );
    }
    let project_id = payload.get("project_id").and_then(Value::as_str);
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    if let Some(project_id) = project_id {
        let exists: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM core_project WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
        )
        .bind(project_id)
        .bind(&tenant_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if exists == 0 {
            return studio_error(
                StatusCode::BAD_REQUEST,
                "PROJECT_INVALID",
                "project_id is invalid.",
            );
        }
    }
    let discovered = match discover(&payload).await {
        Ok(discovered) => discovered,
        Err(response) => return response,
    };
    let all_names: Vec<String> = discovered
        .capabilities
        .iter()
        .filter_map(|entry| {
            entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    let selected: Vec<String> = match payload.get("selected_capabilities") {
        None | Some(Value::Null) => all_names.clone(),
        Some(Value::Array(items)) => {
            let list: Vec<String> = items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect();
            if list.is_empty()
                || list.len() > MAX_TOOLS
                || list.iter().any(|item| !all_names.contains(item))
            {
                return studio_error(
                    StatusCode::BAD_REQUEST,
                    "PLUGIN_CAPABILITIES_INVALID",
                    "selected_capabilities must be a non-empty subset of discovered tools.",
                );
            }
            let mut unique = Vec::new();
            for item in list {
                if !unique.contains(&item) {
                    unique.push(item);
                }
            }
            unique
        }
        Some(_) => {
            return studio_error(
                StatusCode::BAD_REQUEST,
                "PLUGIN_CAPABILITIES_INVALID",
                "selected_capabilities must be a list.",
            )
        }
    };
    let mut manifest = discovered.manifest.clone();
    if let Some(object) = manifest.as_object_mut() {
        if let Some(capabilities) = object.get_mut("capabilities").and_then(Value::as_array_mut) {
            capabilities.retain(|entry| {
                entry
                    .get("name")
                    .and_then(Value::as_str)
                    .map(|name| selected.iter().any(|item| item == name))
                    .unwrap_or(false)
            });
        }
    }
    let schema_fingerprint = fingerprint(&manifest);
    if let Some(object) = manifest.as_object_mut() {
        if let Some(studio) = object.get_mut("studio").and_then(Value::as_object_mut) {
            studio.insert(
                "schema_fingerprint".into(),
                Value::String(schema_fingerprint.clone()),
            );
        }
    }
    let latest = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, version, manifest FROM core_agentplugindefinition \
         WHERE CAST(tenant_id AS TEXT) = ? AND plugin_key = ? ORDER BY created_at DESC LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(plugin_key)
    .fetch_optional(pool)
    .await
    {
        Ok(row) => row,
        Err(error) => return database_error(error.to_string()),
    };
    let unchanged = latest
        .as_ref()
        .map(|row| {
            row.try_get::<String, _>("manifest")
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .map(|value| fingerprint(&value) == schema_fingerprint)
                .unwrap_or(false)
        })
        .unwrap_or(false);
    let plugin_id = if let (true, Some(row)) = (unchanged, latest.as_ref()) {
        row.try_get::<String, _>("id").unwrap_or_default()
    } else {
        let previous_version = latest
            .as_ref()
            .and_then(|row| row.try_get::<String, _>("version").ok())
            .unwrap_or_default();
        let version = payload
            .get("version")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| next_version(&previous_version));
        let id = uuid::Uuid::new_v4().simple().to_string();
        let now = if state
            .config
            .database_url
            .as_deref()
            .unwrap_or("")
            .starts_with("postgres")
        {
            "NOW()"
        } else {
            "datetime('now')"
        };
        let insert = format!(
            "INSERT INTO core_agentplugindefinition (id, tenant_id, created_by_id, plugin_key, name, \
             description, version, protocol, endpoint_url, auth_required, credential_ref, capabilities, \
             manifest, input_schema, output_schema, timeout_seconds, max_concurrency, enabled, \
             created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, '{{}}', '{{}}', 30, 4, 1, {now}, {now})"
        );
        if let Err(error) = sqlx::query(&insert)
            .bind(&id)
            .bind(&tenant_id)
            .bind(user_id)
            .bind(plugin_key)
            .bind(name)
            .bind(description)
            .bind(&version)
            .bind(discovered.protocol)
            .bind(&discovered.endpoint_url)
            .bind(serde_json::to_string(&selected).unwrap_or_else(|_| "[]".into()))
            .bind(serde_json::to_string(&manifest).unwrap_or_default())
            .execute(pool)
            .await
        {
            return database_error(error.to_string());
        }
        id_after_insert(&id, &version)
            .map(|_| id)
            .unwrap_or_default()
    };
    let installation = if let Some(project_id) = project_id {
        let installation_id = uuid::Uuid::new_v4().simple().to_string();
        let now = if state
            .config
            .database_url
            .as_deref()
            .unwrap_or("")
            .starts_with("postgres")
        {
            "NOW()"
        } else {
            "datetime('now')"
        };
        let version: String = sqlx::query_scalar(
            "SELECT version FROM core_agentplugindefinition WHERE CAST(id AS TEXT) = ? LIMIT 1",
        )
        .bind(&plugin_id)
        .fetch_one(pool)
        .await
        .unwrap_or_default();
        let update = format!(
            "UPDATE core_agentplugininstallation SET plugin_id = ?, plugin_version = ?, status = 'installed', \
             granted_capabilities = ?, updated_at = {now} WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
             AND CAST(plugin_id AS TEXT) IN (SELECT CAST(id AS TEXT) FROM core_agentplugindefinition WHERE plugin_key = ?)"
        );
        let existing: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM core_agentplugininstallation WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? AND CAST(plugin_id AS TEXT) = ?",
        )
        .bind(&tenant_id)
        .bind(project_id)
        .bind(&plugin_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if existing == 0 {
            let _ = sqlx::query(
                "INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, plugin_id, plugin_version, status, granted_capabilities, credential_ref, installed_by_id, revision, created_at, updated_at) \
                 VALUES (?, ?, ?, ?, ?, 'installed', ?, '', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            )
            .bind(&installation_id)
            .bind(&tenant_id)
            .bind(project_id)
            .bind(&plugin_id)
            .bind(&version)
            .bind(serde_json::to_string(&selected).unwrap_or_else(|_| "[]".into()))
            .bind(user_id)
            .execute(pool)
            .await;
        } else {
            let _ = sqlx::query(&update)
                .bind(&plugin_id)
                .bind(&version)
                .bind(serde_json::to_string(&selected).unwrap_or_else(|_| "[]".into()))
                .bind(&tenant_id)
                .bind(project_id)
                .bind(plugin_key)
                .execute(pool)
                .await;
        }
        Some(json!({
            "id": installation_id,
            "project": project_id,
            "plugin": plugin_id,
            "plugin_version": version,
            "status": "installed",
            "granted_capabilities": selected,
        }))
    } else {
        None
    };
    let version: String = sqlx::query_scalar(
        "SELECT version FROM core_agentplugindefinition WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&plugin_id)
    .fetch_one(pool)
    .await
    .unwrap_or_default();
    (
        if unchanged {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        Json(json!({
            "definition": {
                "id": plugin_id,
                "plugin_key": plugin_key,
                "name": name,
                "version": version,
                "protocol": discovered.protocol,
                "endpoint_url": discovered.endpoint_url,
                "capabilities": selected,
            },
            "installation": installation,
            "schema_fingerprint": schema_fingerprint,
            "changed": !unchanged,
            "version": version,
        })),
    )
        .into_response()
}

fn id_after_insert(_id: &str, _version: &str) -> Option<()> {
    Some(())
}

/// AES-GCM sealing compatible with `core.field_crypto`: the key is
/// `sha256(f"{scope}:{SECRET_KEY}")`, the nonce is the first 12 bytes of the
/// blob and the AAD is the scope.
pub(crate) fn seal_scope(secret: &str, scope: &str, plaintext: &str) -> Result<String, Response> {
    use aes_gcm::aead::{Aead, KeyInit, Payload};
    use aes_gcm::{Aes256Gcm, Nonce};
    use sha2::Digest;
    let key = sha2::Sha256::digest(format!("{scope}:{secret}").as_bytes());
    let cipher = Aes256Gcm::new_from_slice(&key)
        .map_err(|_| database_error("invalid plugin credential key".into()))?;
    let uuid = uuid::Uuid::new_v4();
    let nonce_bytes = &uuid.as_bytes()[..12];
    let nonce = Nonce::from_slice(nonce_bytes);
    let ciphertext = cipher
        .encrypt(
            nonce,
            Payload {
                msg: plaintext.as_bytes(),
                aad: scope.as_bytes(),
            },
        )
        .map_err(|_| database_error("failed to seal the plugin credential".into()))?;
    let mut blob = nonce_bytes.to_vec();
    blob.extend_from_slice(&ciphertext);
    Ok(base64::engine::general_purpose::URL_SAFE.encode(blob))
}

pub(crate) fn open_scope(secret: &str, scope: &str, blob: &str) -> Option<String> {
    use aes_gcm::aead::{Aead, KeyInit, Payload};
    use aes_gcm::{Aes256Gcm, Nonce};
    use sha2::Digest;
    let key = sha2::Sha256::digest(format!("{scope}:{secret}").as_bytes());
    let cipher = Aes256Gcm::new_from_slice(&key).ok()?;
    let raw = base64::engine::general_purpose::URL_SAFE
        .decode(blob)
        .ok()?;
    if raw.len() < 12 {
        return None;
    }
    let nonce = Nonce::from_slice(&raw[..12]);
    let plaintext = cipher
        .decrypt(
            nonce,
            Payload {
                msg: &raw[12..],
                aad: scope.as_bytes(),
            },
        )
        .ok()?;
    String::from_utf8(plaintext).ok()
}

fn encode_component(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(*byte as char)
            }
            b' ' => encoded.push('+'),
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

pub async fn oauth_authorize(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let user_id = match authorize(&state, &headers, &tenant_id, true).await {
        Ok(id) => id,
        Err(response) => return response,
    };
    let Some(secret) = state.config.platform_secret_key.as_deref() else {
        return database_error("Platform secret key is not configured.".into());
    };
    let field = |name: &str| {
        payload
            .get(name)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string()
    };
    let authorization_url = field("authorization_url");
    let token_url = field("token_url");
    let redirect_uri = field("redirect_uri");
    let client_id = field("client_id");
    if authorization_url.is_empty()
        || token_url.is_empty()
        || redirect_uri.is_empty()
        || client_id.is_empty()
    {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "authorization_url, token_url, redirect_uri and client_id are required.",
        );
    }
    let scope_value = payload.get("scope").and_then(Value::as_str).unwrap_or("");
    let client_secret = payload
        .get("client_secret")
        .and_then(Value::as_str)
        .unwrap_or("");
    let verifier = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
    .chars()
    .take(64)
    .collect::<String>();
    use sha2::Digest;
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .encode(sha2::Sha256::digest(verifier.as_bytes()));
    let expires = chrono::Utc::now() + chrono::Duration::seconds(600);
    let state_payload = json!({
        "version": 1,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "authorization_url": authorization_url,
        "token_url": token_url,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope_value,
        "code_verifier": verifier,
        "expires_at": expires.to_rfc3339(),
    });
    let encoded_state = match seal_scope(
        secret,
        "agent-plugin-oauth-state",
        &serde_json::to_string(&state_payload).unwrap_or_default(),
    ) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let separator = if authorization_url.contains('?') {
        '&'
    } else {
        '?'
    };
    let query = format!(
        "response_type=code&client_id={}&redirect_uri={}&scope={}&state={}&code_challenge={}&code_challenge_method=S256",
        encode_component(&client_id),
        encode_component(&redirect_uri),
        encode_component(scope_value),
        encode_component(&encoded_state),
        encode_component(&challenge),
    );
    Json(json!({
        "authorize_url": format!("{authorization_url}{separator}{query}"),
        "state": encoded_state,
        "expires_in": 600,
    }))
    .into_response()
}

async fn complete_oauth(
    state: &AppState,
    headers: &HeaderMap,
    tenant_id: &str,
    code: &str,
    encoded_state: &str,
) -> Response {
    let user_id = match authorize(state, headers, tenant_id, false).await {
        Ok(id) => id,
        Err(response) => return response,
    };
    let Some(secret) = state.config.platform_secret_key.as_deref() else {
        return database_error("Platform secret key is not configured.".into());
    };
    if code.trim().is_empty() || encoded_state.trim().is_empty() {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "The authorization code and state are required.",
        );
    }
    let Some(plaintext) = open_scope(secret, "agent-plugin-oauth-state", encoded_state) else {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "The OAuth state could not be read.",
        );
    };
    let pending: Value = match serde_json::from_str(&plaintext) {
        Ok(value) => value,
        Err(_) => {
            return studio_error(
                StatusCode::BAD_REQUEST,
                "PLUGIN_AUTH_INVALID",
                "The OAuth state is invalid.",
            )
        }
    };
    if pending.get("version").and_then(Value::as_i64) != Some(1) {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "The OAuth state is invalid.",
        );
    }
    let expires = pending
        .get("expires_at")
        .and_then(Value::as_str)
        .and_then(|value| chrono::DateTime::parse_from_rfc3339(value).ok());
    if expires
        .map(|value| value < chrono::Utc::now())
        .unwrap_or(true)
    {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_EXPIRED",
            "The OAuth state has expired.",
        );
    }
    if pending.get("tenant_id").and_then(Value::as_str) != Some(tenant_id)
        || pending.get("user_id").and_then(Value::as_i64) != Some(user_id)
    {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_STATE_MISMATCH",
            "The OAuth state belongs to another workspace or user.",
        );
    }
    let token_url = pending
        .get("token_url")
        .and_then(Value::as_str)
        .unwrap_or("");
    let client_id = pending
        .get("client_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let client_secret = pending
        .get("client_secret")
        .and_then(Value::as_str)
        .unwrap_or("");
    let redirect_uri = pending
        .get("redirect_uri")
        .and_then(Value::as_str)
        .unwrap_or("");
    let scope_value = pending.get("scope").and_then(Value::as_str).unwrap_or("");
    let verifier = pending
        .get("code_verifier")
        .and_then(Value::as_str)
        .unwrap_or("");
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
    {
        Ok(client) => client,
        Err(error) => return database_error(error.to_string()),
    };
    let response = match client
        .post(token_url)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", code.trim()),
            ("redirect_uri", redirect_uri),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("code_verifier", verifier),
        ])
        .send()
        .await
    {
        Ok(response) => response,
        Err(error) => {
            return studio_error(
                StatusCode::BAD_GATEWAY,
                "PLUGIN_AUTH_FAILED",
                &format!("The token endpoint could not be reached: {error}"),
            )
        }
    };
    let status = response.status();
    let body: Value = response.json().await.unwrap_or_else(|_| json!({}));
    if !status.is_success() {
        return studio_error(
            StatusCode::BAD_GATEWAY,
            if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
                "PLUGIN_AUTH_REJECTED"
            } else {
                "PLUGIN_AUTH_FAILED"
            },
            "The token endpoint rejected the authorization code.",
        );
    }
    let refresh_token = body
        .get("refresh_token")
        .and_then(Value::as_str)
        .unwrap_or("");
    if refresh_token.is_empty() {
        return studio_error(
            StatusCode::BAD_REQUEST,
            "PLUGIN_AUTH_INVALID",
            "The provider did not issue a refresh token; offline access is required.",
        );
    }
    let access_token = body
        .get("access_token")
        .and_then(Value::as_str)
        .unwrap_or("");
    let expires_in = body
        .get("expires_in")
        .and_then(Value::as_i64)
        .unwrap_or(3600);
    let token_type = body
        .get("token_type")
        .and_then(Value::as_str)
        .unwrap_or("Bearer");
    let contract = json!({
        "type": "oauth2",
        "grant": "refresh_token",
        "token_url": token_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope_value,
        "redirect_uri": redirect_uri,
        "refresh_token": refresh_token,
        "access_token": access_token,
        "access_token_expires_at": (chrono::Utc::now() + chrono::Duration::seconds(expires_in)).to_rfc3339(),
    });
    let sealed = match seal_scope(
        secret,
        "agent-plugin-credential",
        &serde_json::to_string(&contract).unwrap_or_default(),
    ) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    let credential_id = uuid::Uuid::new_v4().simple().to_string();
    let now = if state
        .config
        .database_url
        .as_deref()
        .unwrap_or("")
        .starts_with("postgres")
    {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let insert = format!(
        "INSERT INTO core_agentplugincredential (id, tenant_id, created_by_id, name, sealed_value, created_at) \
         VALUES (?, ?, ?, 'OAuth plugin credential', ?, {now})"
    );
    if let Err(error) = sqlx::query(&insert)
        .bind(&credential_id)
        .bind(tenant_id)
        .bind(user_id)
        .bind(&sealed)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    Json(json!({
        "credential": format!("plugin://{credential_id}"),
        "expires_in": expires_in,
        "scope": scope_value,
        "token_type": token_type,
    }))
    .into_response()
}

pub async fn oauth_callback_get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    axum::extract::Query(params): axum::extract::Query<std::collections::BTreeMap<String, String>>,
) -> Response {
    let code = params.get("code").map(String::as_str).unwrap_or("");
    let encoded_state = params.get("state").map(String::as_str).unwrap_or("");
    complete_oauth(&state, &headers, &tenant_id, code, encoded_state).await
}

pub async fn oauth_callback_post(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let code = payload.get("code").and_then(Value::as_str).unwrap_or("");
    let encoded_state = payload.get("state").and_then(Value::as_str).unwrap_or("");
    complete_oauth(&state, &headers, &tenant_id, code, encoded_state).await
}

/// Re-run discovery for an already published plugin and update its stored
/// manifest when the remote surface changed.
pub async fn discover_published_plugin(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, plugin_id)): Path<(String, String)>,
) -> Response {
    let user_id = match authorize(&state, &headers, &tenant_id, true).await {
        Ok(id) => id,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    let row = match sqlx::query(
        "SELECT plugin_key, version, protocol, endpoint_url, manifest FROM core_agentplugindefinition \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&plugin_id)
    .bind(&tenant_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return studio_error(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => return database_error(error.to_string()),
    };
    let endpoint_url: String = row.try_get("endpoint_url").unwrap_or_default();
    let manifest_text: String = row.try_get("manifest").unwrap_or_default();
    let manifest: Value = serde_json::from_str(&manifest_text).unwrap_or_else(|_| json!({}));
    let plugin_type = manifest
        .pointer("/studio/plugin_type")
        .and_then(Value::as_str)
        .unwrap_or("openapi");
    let mut payload = json!({
        "plugin_type": plugin_type,
        "base_url": endpoint_url,
    });
    if let Some(document_url) = manifest
        .pointer("/studio/document_url")
        .and_then(Value::as_str)
    {
        if document_url != "inline://openapi.json" {
            payload["document_url"] = Value::String(document_url.to_string());
        }
    }
    let discovered = match discover(&payload).await {
        Ok(discovered) => discovered,
        Err(response) => return response,
    };
    let schema_fingerprint = fingerprint(&discovered.manifest);
    let previous_fingerprint = fingerprint(&manifest);
    if previous_fingerprint != schema_fingerprint {
        let capabilities: Vec<String> = discovered
            .capabilities
            .iter()
            .filter_map(|entry| {
                entry
                    .get("name")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .collect();
        let now = if state
            .config
            .database_url
            .as_deref()
            .unwrap_or("")
            .starts_with("postgres")
        {
            "NOW()"
        } else {
            "datetime('now')"
        };
        let update = format!(
            "UPDATE core_agentplugindefinition SET capabilities = ?, manifest = ?, updated_at = {now} \
             WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
        );
        if let Err(error) = sqlx::query(&update)
            .bind(serde_json::to_string(&capabilities).unwrap_or_else(|_| "[]".into()))
            .bind(serde_json::to_string(&discovered.manifest).unwrap_or_default())
            .bind(&plugin_id)
            .bind(&tenant_id)
            .execute(pool)
            .await
        {
            return database_error(error.to_string());
        }
    }
    let plugin_key: String = row.try_get("plugin_key").unwrap_or_default();
    let version: String = row.try_get("version").unwrap_or_default();
    Json(json!({
        "definition": {
            "id": plugin_id,
            "plugin_key": plugin_key,
            "version": version,
            "protocol": discovered.protocol,
            "endpoint_url": discovered.endpoint_url,
            "capabilities": discovered.capabilities,
        },
        "schema_fingerprint": schema_fingerprint,
        "changed": previous_fingerprint != schema_fingerprint,
        "connection": discovered.connection,
        "updated_by": user_id,
    }))
    .into_response()
}
