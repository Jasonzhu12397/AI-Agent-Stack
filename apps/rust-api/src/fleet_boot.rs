#![allow(clippy::result_large_err)]

use std::net::IpAddr;
use std::time::Duration;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

const MAX_MACHINES: usize = 100;
const CONFIRMATION: &str = "PXE_ONCE";
const BOOTABLE: [&str; 3] = ["discovered", "identity_review", "commissioned"];

fn fail(status: StatusCode, message: &str, code: &str, details: Value) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": details}})),
    )
        .into_response()
}

fn text(row: &sqlx::any::AnyRow, column: &str) -> String {
    row.try_get::<Option<String>, _>(column)
        .ok()
        .flatten()
        .unwrap_or_default()
}

fn bmc_base(endpoint: &str) -> Result<String, &'static str> {
    let (scheme, rest) = endpoint
        .split_once("://")
        .ok_or("PXE_BOOT_BMC_ADDRESS_INVALID")?;
    if scheme != "https" {
        return Err("PXE_BOOT_BMC_ADDRESS_INVALID");
    }
    let authority = rest.split('/').next().unwrap_or_default();
    let host = authority.rsplit('@').next().unwrap_or_default();
    let host = host
        .strip_prefix('[')
        .and_then(|value| value.split_once(']').map(|(inner, _)| inner))
        .unwrap_or_else(|| host.split(':').next().unwrap_or_default());
    let address: IpAddr = host.parse().map_err(|_| "PXE_BOOT_BMC_ADDRESS_INVALID")?;
    let usable = match address {
        IpAddr::V4(value) => value.is_private(),
        IpAddr::V6(value) => value.is_unique_local(),
    };
    if !usable {
        return Err("PXE_BOOT_BMC_ADDRESS_INVALID");
    }
    Ok(format!("https://{authority}"))
}

async fn vault_secret(reference: &str) -> Result<String, String> {
    let rest = reference
        .strip_prefix("vault://")
        .ok_or("BMC_CREDENTIAL_REFERENCE_INVALID")?;
    let (mount, path) = rest
        .split_once('/')
        .ok_or("BMC_CREDENTIAL_REFERENCE_INVALID")?;
    if mount.is_empty() || path.is_empty() || path.contains("..") || path.contains('?') {
        return Err("BMC_CREDENTIAL_REFERENCE_INVALID".into());
    }
    let address = std::env::var("BMC_VAULT_ADDR").unwrap_or_default();
    let token = std::env::var("BMC_VAULT_TOKEN").unwrap_or_default();
    if address.is_empty() || token.is_empty() {
        return Err("BMC_VAULT_NOT_CONFIGURED".into());
    }
    let response = reqwest::Client::new()
        .get(format!(
            "{}/v1/{}/data/{}",
            address.trim_end_matches('/'),
            mount,
            path
        ))
        .header("X-Vault-Token", token)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|_| "BMC_CREDENTIAL_LOOKUP_FAILED".to_string())?;
    if !response.status().is_success() {
        return Err("BMC_CREDENTIAL_LOOKUP_FAILED".into());
    }
    let body: Value = response
        .json()
        .await
        .map_err(|_| "BMC_CREDENTIAL_LOOKUP_FAILED")?;
    body.pointer("/data/data")
        .cloned()
        .map(|value| value.to_string())
        .ok_or_else(|| "BMC_CREDENTIAL_LOOKUP_FAILED".into())
}

async fn redfish(
    client: &reqwest::Client,
    method: reqwest::Method,
    url: &str,
    credential: &(String, String),
    body: Option<Value>,
) -> Result<Value, String> {
    let mut request = client
        .request(method, url)
        .basic_auth(&credential.0, Some(&credential.1))
        .header("accept", "application/json");
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request
        .send()
        .await
        .map_err(|_| "PXE_BOOT_REQUEST_FAILED".to_string())?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err("PXE_BOOT_AUTH_FAILED".into());
    }
    if response.status().is_redirection() {
        return Err("PXE_BOOT_REDIRECT_REFUSED".into());
    }
    if !response.status().is_success() {
        return Err("PXE_BOOT_REQUEST_FAILED".into());
    }
    if response.content_length().unwrap_or(0) > 1_048_576 {
        return Err("PXE_BOOT_RESPONSE_INVALID".into());
    }
    let bytes = response
        .bytes()
        .await
        .map_err(|_| "PXE_BOOT_REQUEST_FAILED".to_string())?;
    if bytes.is_empty() {
        return Ok(Value::Null);
    }
    serde_json::from_slice(&bytes).map_err(|_| "PXE_BOOT_RESPONSE_INVALID".into())
}

async fn boot_one(endpoint: &str, credential_ref: &str, execute: bool) -> Result<Value, String> {
    let base = bmc_base(endpoint).map_err(str::to_string)?;
    let secret_json = vault_secret(credential_ref).await?;
    let secret: Value = serde_json::from_str(&secret_json)
        .map_err(|_| "PXE_BOOT_CREDENTIAL_INVALID".to_string())?;
    let username = secret
        .get("username")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let password = secret
        .get("password")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if username.is_empty() || password.is_empty() {
        return Err("PXE_BOOT_CREDENTIAL_INVALID".into());
    }
    let credential = (username, password);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
        .map_err(|_| "PXE_BOOT_REQUEST_FAILED".to_string())?;
    let collection = redfish(
        &client,
        reqwest::Method::GET,
        &format!("{base}/redfish/v1/Systems"),
        &credential,
        None,
    )
    .await?;
    let members = collection
        .get("Members")
        .and_then(Value::as_array)
        .ok_or("PXE_BOOT_SYSTEM_MEMBER_COUNT_INVALID")?;
    if members.len() != 1 {
        return Err("PXE_BOOT_SYSTEM_MEMBER_COUNT_INVALID".into());
    }
    let path = members[0]
        .get("@odata.id")
        .and_then(Value::as_str)
        .ok_or("PXE_BOOT_SYSTEM_PATH_INVALID")?;
    let valid_path = path.starts_with("/redfish/v1/Systems/")
        && path.trim_start_matches("/redfish/v1/Systems/").len() <= 64
        && !path.contains("..")
        && !path.contains('?');
    if !valid_path {
        return Err("PXE_BOOT_SYSTEM_PATH_INVALID".into());
    }
    let system = redfish(
        &client,
        reqwest::Method::GET,
        &format!("{base}{path}"),
        &credential,
        None,
    )
    .await?;
    let supported: Vec<String> = system
        .pointer("/Boot/BootSourceOverrideSupported")
        .and_then(Value::as_array)
        .ok_or("PXE_BOOT_UNSUPPORTED")?
        .iter()
        .filter_map(Value::as_str)
        .map(str::to_lowercase)
        .collect();
    if !supported.iter().any(|value| value == "pxe") {
        return Err("PXE_BOOT_UNSUPPORTED".into());
    }
    let power = system
        .get("PowerState")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if power.is_empty() {
        return Err("PXE_BOOT_POWER_STATE_UNKNOWN".into());
    }
    let reset_type = if power.eq_ignore_ascii_case("off") {
        "On"
    } else {
        "ForceRestart"
    };
    let mut evidence = json!({
        "available": true,
        "execute": execute,
        "system_path": path,
        "system_uuid": system.get("UUID").cloned().unwrap_or(Value::Null),
        "manufacturer": system.get("Manufacturer").cloned().unwrap_or(Value::Null),
        "model": system.get("Model").cloned().unwrap_or(Value::Null),
        "boot_patch": {"Boot": {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}},
        "reset_type": reset_type,
        "power_state": power,
        "supported_targets": supported,
    });
    if execute {
        redfish(
            &client,
            reqwest::Method::PATCH,
            &format!("{base}{path}"),
            &credential,
            Some(json!({"Boot": {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}})),
        )
        .await?;
        let reset = redfish(
            &client,
            reqwest::Method::POST,
            &format!("{base}{path}/Actions/ComputerSystem.Reset"),
            &credential,
            Some(json!({"ResetType": reset_type})),
        )
        .await?;
        evidence["boot_override"] = json!("Pxe/Once");
        evidence["reset_response"] = reset;
    }
    Ok(evidence)
}

struct Machine {
    id: String,
    name: String,
    endpoint: String,
    credential_ref: String,
    state: String,
}

async fn machines(pool: &AnyPool, tenant: &str, project: &str) -> Result<Vec<Machine>, String> {
    let rows = sqlx::query(
        "SELECT CAST(m.id AS TEXT) AS id, COALESCE(n.name, '') AS name, COALESCE(m.bmc_endpoint, '') AS endpoint, \
         COALESCE(m.bmc_credential_ref, '') AS credential_ref, COALESCE(m.lifecycle_state, '') AS state \
         FROM core_agentmachine m LEFT JOIN core_agentnode n ON n.id = m.agent_node_id \
         WHERE CAST(m.tenant_id AS TEXT) = ? AND CAST(m.project_id AS TEXT) = ? ORDER BY n.name, m.id LIMIT 100",
    )
    .bind(tenant)
    .bind(project)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| Machine {
            id: text(&row, "id"),
            name: text(&row, "name"),
            endpoint: text(&row, "endpoint"),
            credential_ref: text(&row, "credential_ref"),
            state: text(&row, "state"),
        })
        .collect())
}

pub async fn pxe_boot(
    State(state): State<AppState>,
    Path((tenant_id, project_id)): Path<(String, String)>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable.",
            "DATABASE_UNAVAILABLE",
            json!({}),
        );
    };
    let admin = crate::billing_api::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
        || crate::tasks::project_editor(pool, &project_id, user.id)
            .await
            .unwrap_or(false);
    if !admin {
        return fail(
            StatusCode::FORBIDDEN,
            "Tenant or project administrator role is required.",
            "PERMISSION_DENIED",
            json!({}),
        );
    }
    if !body.is_object() {
        return fail(
            StatusCode::BAD_REQUEST,
            "Request body must be a JSON object.",
            "INVALID_BODY",
            json!({}),
        );
    }
    let execute = body
        .get("execution")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if execute && body.get("confirm").and_then(Value::as_str) != Some(CONFIRMATION) {
        return fail(
            StatusCode::BAD_REQUEST,
            "execution requires confirm=PXE_ONCE.",
            "EXECUTION_CONFIRMATION_REQUIRED",
            json!({"confirmation": CONFIRMATION}),
        );
    }
    let all = match machines(pool, &tenant_id, &project_id).await {
        Ok(rows) => rows,
        Err(code) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                &code,
                "DATABASE_ERROR",
                json!({}),
            )
        }
    };
    let selected: Vec<Machine> = match body.get("machine_ids") {
        None => all
            .into_iter()
            .filter(|m| {
                BOOTABLE.contains(&m.state.as_str())
                    && !m.endpoint.is_empty()
                    && !m.credential_ref.is_empty()
            })
            .collect(),
        Some(Value::Array(ids)) => {
            if ids.is_empty()
                || ids.len() > MAX_MACHINES
                || ids
                    .iter()
                    .any(|id| uuid::Uuid::parse_str(id.as_str().unwrap_or_default()).is_err())
            {
                return fail(
                    StatusCode::BAD_REQUEST,
                    "machine_ids must be a non-empty list of up to 100 UUIDs.",
                    "INVALID_MACHINE_IDS",
                    json!({}),
                );
            }
            let ids: Vec<String> = ids
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect();
            all.into_iter().filter(|m| ids.contains(&m.id)).collect()
        }
        Some(_) => {
            return fail(
                StatusCode::BAD_REQUEST,
                "machine_ids must be a non-empty list of up to 100 UUIDs.",
                "INVALID_MACHINE_IDS",
                json!({}),
            )
        }
    };
    if selected.is_empty() {
        return fail(
            StatusCode::NOT_FOUND,
            "No machines with BMC configuration found.",
            "MACHINES_NOT_FOUND",
            json!({}),
        );
    }
    let blocked: Vec<Value> = selected
        .iter()
        .filter(|m| !BOOTABLE.contains(&m.state.as_str()))
        .map(|m| json!({"machine_id": m.id, "name": m.name, "lifecycle_state": m.state}))
        .collect();
    if !blocked.is_empty() {
        return fail(
            StatusCode::CONFLICT,
            "Machines must be in a bootable lifecycle state for this action.",
            "MACHINE_NOT_BOOTABLE",
            json!({"action": "pxe_boot", "machines": blocked}),
        );
    }
    let mut results = Vec::new();
    let mut executed = 0;
    for machine in &selected {
        if machine.endpoint.is_empty() || machine.credential_ref.is_empty() {
            results.push(json!({"machine_id": machine.id, "name": machine.name, "status": "skipped", "error_code": "PXE_BOOT_BMC_NOT_CONFIGURED"}));
            continue;
        }
        match boot_one(&machine.endpoint, &machine.credential_ref, execute).await {
            Ok(evidence) => {
                if execute { executed += 1; }
                results.push(json!({"machine_id": machine.id, "name": machine.name, "status": if execute {"executed"} else {"planned"}, "evidence": evidence}));
            }
            Err(code) => results.push(json!({"machine_id": machine.id, "name": machine.name, "status": "failed", "error_code": code})),
        }
    }
    (StatusCode::OK, Json(json!({"execution": execute, "machine_count": selected.len(), "executed": executed, "results": results}))).into_response()
}

async fn online_eph_nodes(
    pool: &AnyPool,
    tenant: &str,
    project: &str,
) -> Result<Vec<(String, String)>, String> {
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, COALESCE(name, '') AS name FROM core_agentnode \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         AND status = 'online' AND node_type = 'eph_node' ORDER BY name, id",
    )
    .bind(tenant)
    .bind(project)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| (text(&row, "id"), text(&row, "name")))
        .collect())
}

pub async fn discovery(
    State(state): State<AppState>,
    Path((tenant_id, project_id)): Path<(String, String)>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable.",
            "DATABASE_UNAVAILABLE",
            json!({}),
        );
    };
    let admin = crate::billing_api::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
        || crate::tasks::project_editor(pool, &project_id, user.id)
            .await
            .unwrap_or(false);
    if !admin {
        return fail(
            StatusCode::FORBIDDEN,
            "Tenant or project administrator role is required.",
            "PERMISSION_DENIED",
            json!({}),
        );
    }
    if !body.is_object() {
        return fail(
            StatusCode::BAD_REQUEST,
            "Request body must be a JSON object.",
            "INVALID_BODY",
            json!({}),
        );
    }
    let execute = body
        .get("execution")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if execute && body.get("confirm").and_then(Value::as_str) != Some(CONFIRMATION) {
        return fail(
            StatusCode::BAD_REQUEST,
            "execution requires confirm=PXE_ONCE.",
            "EXECUTION_CONFIRMATION_REQUIRED",
            json!({"confirmation": CONFIRMATION}),
        );
    }
    let all = match machines(pool, &tenant_id, &project_id).await {
        Ok(rows) => rows,
        Err(code) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                &code,
                "DATABASE_ERROR",
                json!({}),
            )
        }
    };
    let selected: Vec<Machine> = match body.get("machine_ids") {
        None => all
            .into_iter()
            .filter(|m| {
                BOOTABLE.contains(&m.state.as_str())
                    && !m.endpoint.is_empty()
                    && !m.credential_ref.is_empty()
            })
            .collect(),
        Some(Value::Array(ids)) => {
            if ids.is_empty()
                || ids.len() > MAX_MACHINES
                || ids
                    .iter()
                    .any(|id| uuid::Uuid::parse_str(id.as_str().unwrap_or_default()).is_err())
            {
                return fail(
                    StatusCode::BAD_REQUEST,
                    "machine_ids must be a non-empty list of up to 100 UUIDs.",
                    "INVALID_MACHINE_IDS",
                    json!({}),
                );
            }
            let ids: Vec<String> = ids
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect();
            all.into_iter().filter(|m| ids.contains(&m.id)).collect()
        }
        Some(_) => {
            return fail(
                StatusCode::BAD_REQUEST,
                "machine_ids must be a non-empty list of up to 100 UUIDs.",
                "INVALID_MACHINE_IDS",
                json!({}),
            )
        }
    };
    if selected.is_empty() {
        return fail(
            StatusCode::NOT_FOUND,
            "No machines with BMC configuration found.",
            "MACHINES_NOT_FOUND",
            json!({}),
        );
    }
    let blocked: Vec<Value> = selected
        .iter()
        .filter(|m| !BOOTABLE.contains(&m.state.as_str()))
        .map(|m| json!({"machine_id": m.id, "name": m.name, "lifecycle_state": m.state}))
        .collect();
    if !blocked.is_empty() {
        return fail(
            StatusCode::CONFLICT,
            "Machines must be in a bootable lifecycle state for this action.",
            "MACHINE_NOT_BOOTABLE",
            json!({"action": "discovery", "machines": blocked}),
        );
    }
    let mut results = Vec::new();
    let mut executed = 0;
    for machine in &selected {
        if machine.endpoint.is_empty() || machine.credential_ref.is_empty() {
            results.push(json!({"machine_id": machine.id, "name": machine.name, "status": "skipped", "error_code": "PXE_BOOT_BMC_NOT_CONFIGURED"}));
            continue;
        }
        match boot_one(&machine.endpoint, &machine.credential_ref, execute).await {
            Ok(evidence) => {
                if execute { executed += 1; }
                results.push(json!({"machine_id": machine.id, "name": machine.name, "status": if execute {"executed"} else {"planned"}, "evidence": evidence}));
            }
            Err(code) => results.push(json!({"machine_id": machine.id, "name": machine.name, "status": "failed", "error_code": code})),
        }
    }
    let mut inspect = json!({"queued": [], "skipped": [], "reason": null});
    if !execute {
        inspect["reason"] = json!("NOT_EXECUTED");
    } else if executed == 0 {
        inspect["reason"] = json!("NO_MACHINE_BOOTED");
    } else {
        let idempotency_key = headers
            .get("idempotency-key")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .trim()
            .to_string();
        if idempotency_key.is_empty() || idempotency_key.len() > 96 {
            return fail(
                StatusCode::BAD_REQUEST,
                "A valid Idempotency-Key header is required.",
                "IDEMPOTENCY_KEY_REQUIRED",
                json!({}),
            );
        }
        let nodes = match online_eph_nodes(pool, &tenant_id, &project_id).await {
            Ok(nodes) => nodes,
            Err(code) => {
                return fail(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    &code,
                    "DATABASE_ERROR",
                    json!({}),
                )
            }
        };
        if nodes.is_empty() {
            inspect["reason"] = json!("EPH_OFFLINE");
        } else {
            let plan_hash =
                crate::agent_runtime_api::payload_hash("metal3.host.inspect", &json!({}));
            let mut queued = Vec::new();
            let mut skipped = Vec::new();
            for (node_id, _) in nodes {
                let key = format!("discovery:{idempotency_key}:{node_id}");
                let existing: Option<String> = sqlx::query_scalar("SELECT CAST(id AS TEXT) FROM core_agentcommand WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1")
                    .bind(&tenant_id).bind(&key).fetch_optional(pool).await.unwrap_or(None);
                if let Some(_command_id) = existing {
                    skipped.push(json!({"node_id": node_id, "reason": "already_queued"}));
                    continue;
                }
                let command_id = uuid::Uuid::new_v4().simple().to_string();
                let sql = "INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, plan_hash, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id) \
                           VALUES (?, 'metal3.host.inspect', '{}', 'queued', ?, ?, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?)";
                match sqlx::query(sql)
                    .bind(&command_id)
                    .bind(&key)
                    .bind(&plan_hash)
                    .bind(&node_id)
                    .bind(&tenant_id)
                    .bind(&project_id)
                    .bind(user.id)
                    .execute(pool)
                    .await
                {
                    Ok(_) => queued.push(json!({"node_id": node_id, "command_id": command_id})),
                    Err(error) => {
                        return fail(
                            StatusCode::INTERNAL_SERVER_ERROR,
                            &error.to_string(),
                            "DATABASE_ERROR",
                            json!({}),
                        )
                    }
                }
            }
            inspect["queued"] = json!(queued);
            inspect["skipped"] = json!(skipped);
        }
    }
    (
        if execute {
            StatusCode::ACCEPTED
        } else {
            StatusCode::OK
        },
        Json(json!({
            "execution": execute,
            "machine_count": selected.len(),
            "executed": executed,
            "results": results,
            "inspect": inspect,
        })),
    )
        .into_response()
}
