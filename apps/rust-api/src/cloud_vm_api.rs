#![allow(clippy::result_large_err)]

//! Native readiness for the operator-configured cloud VM console surface.

use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use sqlx::Row;

use crate::proxy::AppState;

fn error_json(status: StatusCode, code: &str, message: &str) -> Response {
    (status, Json(json!({"detail": message, "code": code}))).into_response()
}

fn is_postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"))
}

fn now_column(state: &AppState, alias: &str, column: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%d %H:%M:%S', {alias}.{column})")
    }
}

/// `GET /api/v1/tenants/{tenant}/projects/{project}/cloud-vms/providers/`
pub async fn list_providers(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Project not found."),
        Err(error) => {
            return error_json(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error)
        }
    }
    let parsed = match parse_providers() {
        Ok(parsed) => parsed,
        Err(message) => {
            return error_json(
                StatusCode::SERVICE_UNAVAILABLE,
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                &message,
            )
        }
    };
    let public: Vec<Value> = parsed
        .into_iter()
        .map(|item| {
            json!({
                "key": item.get("key").cloned().unwrap_or(Value::Null),
                "type": item.get("type").cloned().unwrap_or(Value::Null),
                "display_name": item.get("display_name").cloned().unwrap_or(Value::Null),
                "region": item.get("region").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Json(json!({"count": public.len(), "results": public})).into_response()
}

/// `GET /api/v1/tenants/{tenant}/projects/{project}/cloud-vms/`
pub async fn get_instance(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vm_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    let scope = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_cloudvminstance WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&vm_id)
    .fetch_optional(pool)
    .await;
    let (tenant_id, project_id) = match scope {
        Ok(Some(row)) => (
            row.try_get::<String, _>("tenant_id").unwrap_or_default(),
            row.try_get::<String, _>("project_id").unwrap_or_default(),
        ),
        Ok(None) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return error_json(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error)
        }
    }
    let created = now_column(&state, "vm", "created_at");
    let updated = now_column(&state, "vm", "updated_at");
    let network_text = if is_postgres(&state) {
        "CAST(vm.network_ids AS TEXT)"
    } else {
        "vm.network_ids"
    };
    let metadata_text = if is_postgres(&state) {
        "CAST(vm.provider_metadata AS TEXT)"
    } else {
        "vm.provider_metadata"
    };
    let sql = format!(
        "SELECT CAST(vm.id AS TEXT) AS id, CAST(vm.tenant_id AS TEXT) AS tenant_id, \
         CAST(vm.project_id AS TEXT) AS project_id, vm.provider_key, vm.server_id, vm.name, \
         vm.flavor_ref, vm.image_ref, {network_text} AS network_text, vm.status, vm.ip_address, \
         {metadata_text} AS metadata_text, {created} AS created, {updated} AS updated \
         FROM core_cloudvminstance vm WHERE CAST(vm.id AS TEXT) = ? LIMIT 1"
    );
    match sqlx::query(&sql).bind(&vm_id).fetch_optional(pool).await {
        Ok(Some(row)) => {
            let networks: Value = row
                .try_get::<String, _>("network_text")
                .ok()
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_else(|| json!([]));
            let metadata: Value = row
                .try_get::<String, _>("metadata_text")
                .ok()
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_else(|| json!({}));
            Json(json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "provider_key": row.try_get::<String, _>("provider_key").unwrap_or_default(),
                "server_id": row.try_get::<String, _>("server_id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "flavor_ref": row.try_get::<String, _>("flavor_ref").unwrap_or_default(),
                "image_ref": row.try_get::<String, _>("image_ref").unwrap_or_default(),
                "network_ids": networks,
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "ip_address": row.try_get::<Option<String>, _>("ip_address").ok().flatten(),
                "provider_metadata": metadata,
                "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                "updated_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
                "warnings": [],
            }))
            .into_response()
        }
        Ok(None) => error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        ),
    }
}

pub async fn list_instances(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Project not found."),
        Err(error) => {
            return error_json(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error)
        }
    };
    let created = now_column(&state, "vm", "created_at");
    let updated = now_column(&state, "vm", "updated_at");
    let network_text = if is_postgres(&state) {
        "CAST(vm.network_ids AS TEXT)"
    } else {
        "vm.network_ids"
    };
    let metadata_text = if is_postgres(&state) {
        "CAST(vm.provider_metadata AS TEXT)"
    } else {
        "vm.provider_metadata"
    };
    let sql = format!(
        "SELECT CAST(vm.id AS TEXT) AS id, CAST(vm.tenant_id AS TEXT) AS tenant_id, \
         CAST(vm.project_id AS TEXT) AS project_id, vm.provider_key, vm.server_id, vm.name, \
         vm.flavor_ref, vm.image_ref, {network_text} AS network_text, vm.status, vm.ip_address, \
         {metadata_text} AS metadata_text, {created} AS created, {updated} AS updated \
         FROM core_cloudvminstance vm WHERE CAST(vm.tenant_id AS TEXT) = ? \
         AND CAST(vm.project_id AS TEXT) = ? ORDER BY vm.created_at DESC LIMIT 100"
    );
    let rows = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&project_id)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let networks: Value = row
                .try_get::<String, _>("network_text")
                .ok()
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_else(|| json!([]));
            let metadata: Value = row
                .try_get::<String, _>("metadata_text")
                .ok()
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_else(|| json!({}));
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "provider_key": row.try_get::<String, _>("provider_key").unwrap_or_default(),
                "server_id": row.try_get::<String, _>("server_id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "flavor_ref": row.try_get::<String, _>("flavor_ref").unwrap_or_default(),
                "image_ref": row.try_get::<String, _>("image_ref").unwrap_or_default(),
                "network_ids": networks,
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "ip_address": row.try_get::<Option<String>, _>("ip_address").ok().flatten(),
                "provider_metadata": metadata,
                "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                "updated_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
            })
        })
        .collect();
    Json(json!({"count": results.len(), "results": results, "warnings": []})).into_response()
}

fn parse_providers() -> Result<Vec<Value>, String> {
    let raw = std::env::var("CLOUD_VM_PROVIDERS").unwrap_or_default();
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    let parsed: Value = serde_json::from_str(raw)
        .map_err(|_| "CLOUD_VM_PROVIDERS is not valid JSON.".to_string())?;
    let entries = match parsed {
        Value::Object(map) => map
            .into_iter()
            .map(|(key, mut value)| {
                if let Value::Object(ref mut object) = value {
                    object.insert("key".to_string(), Value::String(key));
                }
                value
            })
            .collect::<Vec<_>>(),
        Value::Array(items) => items,
        _ => return Err("CLOUD_VM_PROVIDERS must be a JSON array or object.".to_string()),
    };
    let mut providers = Vec::new();
    for entry in entries {
        if !entry.is_object() {
            return Err("Each provider entry must be an object.".to_string());
        }
        let key = entry.get("key").and_then(Value::as_str).unwrap_or_default();
        if !valid_key(key) {
            return Err("Provider keys must be unique lowercase identifiers.".to_string());
        }
        if entry.get("type").and_then(Value::as_str) != Some("openstack") {
            return Err(format!("Provider {key} has an unsupported type."));
        }
        if providers
            .iter()
            .any(|item: &Value| item.get("key").and_then(Value::as_str) == Some(key))
        {
            return Err("Provider keys must be unique lowercase identifiers.".to_string());
        }
        providers.push(entry);
    }
    Ok(providers)
}

fn valid_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .chars()
            .next()
            .map(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit())
            .unwrap_or(false)
        && value
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
}

fn provider_entry(key: &str) -> Result<Value, Response> {
    let providers = parse_providers().map_err(|message| {
        error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "CLOUD_VM_PROVIDER_CONFIG_INVALID",
            &message,
        )
    })?;
    providers
        .into_iter()
        .find(|entry| entry.get("key").and_then(Value::as_str) == Some(key))
        .ok_or_else(|| {
            error_json(
                StatusCode::NOT_FOUND,
                "CLOUD_VM_PROVIDER_UNKNOWN",
                &format!("Cloud provider {key:?} is not configured."),
            )
        })
}

fn required_provider_text(entry: &Value, field: &str) -> Result<String, Response> {
    entry
        .get(field)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| {
            error_json(
                StatusCode::SERVICE_UNAVAILABLE,
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                &format!("{field} is required."),
            )
        })
}

async fn provider_action(entry: &Value, server_id: &str, action: &str) -> Result<(), Response> {
    let auth_url = required_provider_text(entry, "auth_url")?;
    let username = required_provider_text(entry, "username")?;
    let password = required_provider_text(entry, "password")?;
    let project_name = required_provider_text(entry, "project_name")?;
    let domain_name = entry
        .get("domain_name")
        .and_then(Value::as_str)
        .unwrap_or("Default")
        .to_string();
    let region = entry
        .get("region")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let verify_tls = entry
        .get("verify_tls")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let timeout = entry.get("timeout").and_then(Value::as_u64).unwrap_or(20);
    let client = reqwest::Client::builder()
        .danger_accept_invalid_certs(!verify_tls)
        .timeout(std::time::Duration::from_secs(timeout))
        .build()
        .map_err(|error| {
            error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                &error.to_string(),
            )
        })?;
    let auth = json!({
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {"user": {
                    "name": username,
                    "domain": {"name": domain_name},
                    "password": password,
                }},
            },
            "scope": {"project": {
                "name": project_name,
                "domain": {"name": domain_name},
            }},
        }
    });
    let response = client
        .post(format!("{auth_url}/auth/tokens"))
        .json(&auth)
        .send()
        .await
        .map_err(|error| {
            error_json(
                StatusCode::SERVICE_UNAVAILABLE,
                "CLOUD_VM_PROVIDER_UNAVAILABLE",
                &format!("Cloud provider is unreachable ({error})."),
            )
        })?;
    if !response.status().is_success() {
        return Err(error_json(
            StatusCode::BAD_GATEWAY,
            "CLOUD_VM_PROVIDER_UNAUTHORIZED",
            &format!("Keystone returned HTTP {}.", response.status()),
        ));
    }
    let token = response
        .headers()
        .get("X-Subject-Token")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string();
    if token.is_empty() {
        return Err(error_json(
            StatusCode::BAD_GATEWAY,
            "CLOUD_VM_PROVIDER_RESPONSE_INVALID",
            "Keystone did not return a subject token.",
        ));
    }
    let body: Value = response.json().await.unwrap_or(json!({}));
    let configured = entry
        .get("compute_url")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut compute_url = configured;
    if compute_url.is_none() {
        if let Some(catalog) = body
            .get("token")
            .and_then(|token| token.get("catalog"))
            .and_then(Value::as_array)
        {
            for service in catalog {
                if service.get("type").and_then(Value::as_str) != Some("compute") {
                    continue;
                }
                let Some(endpoints) = service.get("endpoints").and_then(Value::as_array) else {
                    continue;
                };
                for endpoint in endpoints {
                    if endpoint.get("interface").and_then(Value::as_str) != Some("public") {
                        continue;
                    }
                    let endpoint_region = endpoint
                        .get("region_id")
                        .or_else(|| endpoint.get("region"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if !region.is_empty() && endpoint_region != region {
                        continue;
                    }
                    if let Some(url) = endpoint.get("url").and_then(Value::as_str) {
                        compute_url = Some(url.trim_end_matches('/').to_string());
                        break;
                    }
                }
            }
        }
    }
    let mut base = compute_url.ok_or_else(|| {
        error_json(
            StatusCode::BAD_GATEWAY,
            "CLOUD_VM_PROVIDER_RESPONSE_INVALID",
            "Provider catalog has no public compute endpoint.",
        )
    })?;
    if !base.rsplit('/').next().is_some_and(|segment| {
        segment.starts_with('v') && segment[1..].chars().all(|c| c.is_ascii_digit() || c == '.')
    }) {
        base = format!("{base}/v2.1");
    }
    let body = match action {
        "start" => json!({"os-start": null}),
        "stop" => json!({"os-stop": null}),
        "reboot" => json!({"reboot": {"type": "SOFT"}}),
        "hard-reboot" => json!({"reboot": {"type": "HARD"}}),
        _ => unreachable!(),
    };
    let response = client
        .post(format!("{base}/servers/{server_id}/action"))
        .header("X-Auth-Token", token)
        .header("Accept", "application/json")
        .json(&body)
        .send()
        .await
        .map_err(|error| {
            error_json(
                StatusCode::SERVICE_UNAVAILABLE,
                "CLOUD_VM_PROVIDER_UNAVAILABLE",
                &format!("Cloud provider is unreachable ({error})."),
            )
        })?;
    if response.status().is_success() {
        return Ok(());
    }
    let status = response.status();
    let message = response.text().await.unwrap_or_default();
    let code = match status.as_u16() {
        404 => "CLOUD_VM_RESOURCE_NOT_FOUND",
        409 => "CLOUD_VM_CONFLICT",
        401 | 403 => "CLOUD_VM_PROVIDER_UNAUTHORIZED",
        _ => "CLOUD_VM_PROVIDER_ERROR",
    };
    let http = match code {
        "CLOUD_VM_RESOURCE_NOT_FOUND" => StatusCode::NOT_FOUND,
        "CLOUD_VM_CONFLICT" => StatusCode::CONFLICT,
        _ => StatusCode::BAD_GATEWAY,
    };
    Err(error_json(
        http,
        code,
        &format!("Cloud provider returned HTTP {status}: {message}"),
    ))
}

/// `POST /api/v1/cloud-vms/{vm_id}/actions/`
pub async fn power_action(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vm_id): Path<String>,
    Json(body): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    let scope = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, \
         provider_key, server_id FROM core_cloudvminstance WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&vm_id)
    .fetch_optional(pool)
    .await;
    let scope = match scope {
        Ok(Some(row)) => row,
        Ok(None) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let tenant_id: String = scope.try_get("tenant_id").unwrap_or_default();
    let project_id: String = scope.try_get("project_id").unwrap_or_default();
    if !crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.");
    }
    let editable = sqlx::query(
        "SELECT 1 FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? AND role IN ('owner', 'admin') \
         UNION SELECT 1 FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ? AND role IN ('admin', 'editor') \
         LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(user.id)
    .bind(&project_id)
    .bind(user.id)
    .fetch_optional(pool)
    .await;
    match editable {
        Ok(Some(_)) => {}
        Ok(None) => {
            return error_json(
                StatusCode::FORBIDDEN,
                "PERMISSION_DENIED",
                "Project editor permission required.",
            )
        }
        Err(error) => {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    }
    let action = body.get("action").and_then(Value::as_str).unwrap_or("");
    if !matches!(action, "start" | "stop" | "reboot" | "hard-reboot") {
        return error_json(
            StatusCode::BAD_REQUEST,
            "CLOUD_VM_ACTION_INVALID",
            "action must be one of start, stop, reboot, hard-reboot.",
        );
    }
    let provider_key: String = scope.try_get("provider_key").unwrap_or_default();
    let server_id: String = scope.try_get("server_id").unwrap_or_default();
    let entry = match provider_entry(&provider_key) {
        Ok(entry) => entry,
        Err(response) => return response,
    };
    if let Err(response) = provider_action(&entry, &server_id, action).await {
        return response;
    }
    let status = match action {
        "start" => "STARTING",
        "stop" => "STOPPING",
        _ => "REBOOTING",
    };
    if let Err(error) = sqlx::query(
        "UPDATE core_cloudvminstance SET status = ?, updated_at = CURRENT_TIMESTAMP \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(status)
    .bind(&vm_id)
    .execute(pool)
    .await
    {
        return error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        );
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or_default(),
        crate::audit::AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "cloud_vm.action",
            resource_type: "cloud_vm",
            resource_id: &vm_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: headers
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default(),
            remote_addr: None,
            user_agent: headers
                .get("user-agent")
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default(),
            metadata: json!({"provider": provider_key, "action": action}),
        },
    )
    .await;
    get_instance(State(state), headers, Path(vm_id)).await
}
