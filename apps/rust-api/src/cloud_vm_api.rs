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
