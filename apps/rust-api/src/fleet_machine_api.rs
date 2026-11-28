//! Fleet machine configuration, verification and firmware endpoints.
//!
//! These handlers replace the retired Python fleet views. They read and write
//! the tables the Rust API owns, call the BMC directly over Redfish and append
//! the same audit events the former views recorded.

#![allow(clippy::result_large_err)]

use std::collections::{BTreeMap, BTreeSet};
use std::net::IpAddr;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sqlx::Row;

use crate::audit::{record_event, AuditEvent};
use crate::context::{authenticate, AuthError, SessionUser};
use crate::proxy::AppState;

const HOST_ROLES: [&str; 3] = ["control_plane", "worker", "storage"];
const SUPPORTED_VENDORS: [&str; 5] = ["HPE", "Dell", "Inspur", "H3C", "Lenovo"];
const REFERENCE_MAX: usize = 160;
const REASON_MIN: usize = 10;
const REASON_MAX: usize = 500;
const FIRMWARE_MAX_JOBS: i64 = 20;

fn error(status: StatusCode, code: &str, message: &str, details: Value) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": details}})),
    )
        .into_response()
}

fn unavailable() -> Response {
    error(
        StatusCode::SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "The service is temporarily unavailable.",
        json!({}),
    )
}

fn db_error(context: &str, value: impl std::fmt::Display) -> Response {
    tracing::error!(error = %value, "{context}");
    unavailable()
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false)
}

fn text(row: &sqlx::any::AnyRow, column: &str) -> String {
    row.try_get::<Option<String>, _>(column)
        .ok()
        .flatten()
        .unwrap_or_default()
}

fn json_value(value: &str, fallback: Value) -> Value {
    serde_json::from_str(value).unwrap_or(fallback)
}

fn postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .map(|url| url.starts_with("postgres://") || url.starts_with("postgresql://"))
        .unwrap_or(false)
}

async fn require_user(state: &AppState, headers: &HeaderMap) -> Result<SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    match authenticate(pool, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(_)) => Err(error(
            StatusCode::UNAUTHORIZED,
            "AUTHENTICATION_REQUIRED",
            "Authentication required.",
            json!({}),
        )),
        Err(AuthError::Unavailable(message)) => Err(db_error("session lookup failed", message)),
    }
}

async fn roles(
    state: &AppState,
    user: i64,
    tenant_id: &str,
    project_id: &str,
) -> Result<(bool, bool, bool), Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    let tenant_admin = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? AND role IN ('owner', 'admin')",
    )
    .bind(tenant_id)
    .bind(user)
    .fetch_one(pool)
    .await
    .map_err(|value| db_error("tenant membership lookup failed", value))?;
    let project_role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user)
    .fetch_optional(pool)
    .await
    .map_err(|value| db_error("project membership lookup failed", value))?;
    let role = project_role.unwrap_or_default();
    Ok((
        tenant_admin > 0,
        role == "admin",
        role == "admin" || role == "editor",
    ))
}

struct Machine {
    id: String,
    tenant_id: String,
    project_id: String,
    name: String,
    vendor: String,
    product_name: String,
    system_uuid: String,
    identity_material: Value,
    identity_status: String,
    identity_conflicts: Vec<Value>,
    hardware_inventory: Value,
    inventory_source: String,
    inventory_revision: i64,
    observed_at: Option<String>,
    endpoint: String,
    credential_ref: String,
    trust_ref: String,
    lifecycle_state: String,
}

async fn machine(
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    machine_id: &str,
) -> Result<Machine, Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    let row = sqlx::query(
        "SELECT CAST(m.id AS TEXT) AS id, CAST(m.tenant_id AS TEXT) AS tenant_id, \
         CAST(m.project_id AS TEXT) AS project_id, COALESCE(n.name, '') AS name, \
         COALESCE(m.vendor, '') AS vendor, COALESCE(m.product_name, '') AS product_name, \
         COALESCE(m.system_uuid, '') AS system_uuid, COALESCE(m.identity_material, '{}') AS identity_material, \
         COALESCE(m.identity_status, '') AS identity_status, COALESCE(m.identity_conflicts, '[]') AS identity_conflicts, \
         COALESCE(m.hardware_inventory, '{}') AS hardware_inventory, \
         COALESCE(m.inventory_source, '') AS inventory_source, COALESCE(m.inventory_revision, 0) AS inventory_revision, \
         CAST(m.observed_at AS TEXT) AS observed_at, COALESCE(m.bmc_endpoint, '') AS endpoint, \
         COALESCE(m.bmc_credential_ref, '') AS credential_ref, \
         COALESCE(m.bmc_trust_bundle_ref, '') AS trust_ref, \
         COALESCE(m.lifecycle_state, '') AS lifecycle_state \
         FROM core_agentmachine m LEFT JOIN core_agentnode n ON n.id = m.agent_node_id \
         WHERE CAST(m.tenant_id AS TEXT) = ? AND CAST(m.project_id AS TEXT) = ? \
         AND CAST(m.id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(project_id)
    .bind(machine_id)
    .fetch_optional(pool)
    .await
    .map_err(|value| db_error("machine lookup failed", value))?;
    let Some(row) = row else {
        return Err(error(
            StatusCode::NOT_FOUND,
            "MACHINE_NOT_FOUND",
            "Machine not found.",
            json!({}),
        ));
    };
    let identity_conflicts = json_value(&text(&row, "identity_conflicts"), json!([]));
    Ok(Machine {
        id: text(&row, "id"),
        tenant_id: text(&row, "tenant_id"),
        project_id: text(&row, "project_id"),
        name: text(&row, "name"),
        vendor: text(&row, "vendor"),
        product_name: text(&row, "product_name"),
        system_uuid: text(&row, "system_uuid"),
        identity_material: json_value(&text(&row, "identity_material"), json!({})),
        identity_status: text(&row, "identity_status"),
        identity_conflicts: identity_conflicts.as_array().cloned().unwrap_or_default(),
        hardware_inventory: json_value(&text(&row, "hardware_inventory"), json!({})),
        inventory_source: text(&row, "inventory_source"),
        inventory_revision: row.try_get::<i64, _>("inventory_revision").unwrap_or(0),
        observed_at: row
            .try_get::<Option<String>, _>("observed_at")
            .ok()
            .flatten(),
        endpoint: text(&row, "endpoint"),
        credential_ref: text(&row, "credential_ref"),
        trust_ref: text(&row, "trust_ref"),
        lifecycle_state: text(&row, "lifecycle_state"),
    })
}

async fn audit(
    state: &AppState,
    machine: &Machine,
    event_type: &str,
    resource_type: &str,
    resource_id: &str,
    metadata: Value,
) {
    let (Some(pool), Some(url)) = (
        state.identity_pool.as_ref(),
        state.config.database_url.as_ref(),
    ) else {
        return;
    };
    if let Err(message) = record_event(
        pool,
        url,
        AuditEvent {
            tenant_id: Some(&machine.tenant_id),
            project_id: Some(&machine.project_id),
            event_type,
            resource_type,
            resource_id,
            outcome: "success",
            actor_id: None,
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        },
    )
    .await
    {
        tracing::error!(%message, "could not append fleet audit event");
    }
}

fn valid_reference(value: &str) -> bool {
    (1..=REFERENCE_MAX).contains(&value.len())
        && value
            .chars()
            .next()
            .map(|first| first.is_ascii_alphanumeric())
            .unwrap_or(false)
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | ':' | '/' | '-')
        })
}

fn valid_bmc_endpoint(value: &str) -> Result<String, String> {
    if value.is_empty() || value.len() > 500 {
        return Err("endpoint must be an HTTPS origin with a valid host.".into());
    }
    let parsed = url::Url::parse(value).map_err(|_| "endpoint is invalid".to_string())?;
    if parsed.scheme() != "https" || parsed.host_str().is_none() {
        return Err("endpoint must be an HTTPS origin with a valid host.".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("endpoint must not embed credentials.".into());
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("endpoint must not contain a query or fragment.".into());
    }
    if !matches!(parsed.path(), "" | "/") {
        return Err("endpoint must be an origin without a path.".into());
    }
    let host = parsed
        .host_str()
        .unwrap_or_default()
        .trim_end_matches('.')
        .to_ascii_lowercase();
    if host == "localhost" || host.ends_with(".localhost") || host.ends_with(".local") {
        return Err("endpoint host is not allowed.".into());
    }
    if let Ok(address) = host.parse::<IpAddr>() {
        let forbidden = match address {
            IpAddr::V4(value) => {
                value.is_loopback()
                    || value.is_unspecified()
                    || value.is_multicast()
                    || value.is_link_local()
            }
            IpAddr::V6(value) => {
                value.is_loopback() || value.is_unspecified() || value.is_multicast()
            }
        };
        if forbidden {
            return Err("endpoint host is not allowed.".into());
        }
    }
    let authority = parsed
        .port()
        .map(|port| format!("{host}:{port}"))
        .unwrap_or(host);
    Ok(format!("https://{authority}"))
}

pub async fn update_bmc_config(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok((tenant_admin, _, project_editor)) =
        roles(&state, user.id, &tenant_id, &project_id).await
    else {
        return unavailable();
    };
    if !tenant_admin && !project_editor {
        return error(
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
            "Project editor or administrator role is required.",
            json!({}),
        );
    }
    let Ok(payload) = parse_object(&body) else {
        return error(
            StatusCode::BAD_REQUEST,
            "INVALID_BODY",
            "Request body must be a JSON object.",
            json!({}),
        );
    };
    let allowed = ["endpoint", "credential_ref", "trust_bundle_ref"];
    if let Some(field) = payload.as_object().and_then(|object| {
        object
            .keys()
            .find(|field| !allowed.contains(&field.as_str()))
    }) {
        return error(
            StatusCode::BAD_REQUEST,
            "UNSUPPORTED_FIELD",
            "Only endpoint and credential references are accepted.",
            json!({"field": field}),
        );
    }
    let endpoint = match payload.get("endpoint").and_then(Value::as_str) {
        Some(value) => match valid_bmc_endpoint(value) {
            Ok(value) => value,
            Err(message) => {
                return error(
                    StatusCode::BAD_REQUEST,
                    "BMC_ENDPOINT_INVALID",
                    &message,
                    json!({}),
                )
            }
        },
        None => {
            return error(
                StatusCode::BAD_REQUEST,
                "BMC_ENDPOINT_INVALID",
                "endpoint must be an HTTPS origin with a valid host.",
                json!({}),
            )
        }
    };
    let credential_ref = payload
        .get("credential_ref")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !valid_reference(credential_ref) {
        return error(
            StatusCode::BAD_REQUEST,
            "BMC_CREDENTIAL_REFERENCE_INVALID",
            "credential_ref must be a vault reference, not a secret.",
            json!({}),
        );
    }
    let trust_ref = payload
        .get("trust_bundle_ref")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !trust_ref.is_empty() && !valid_reference(trust_ref) {
        return error(
            StatusCode::BAD_REQUEST,
            "BMC_TRUST_BUNDLE_REFERENCE_INVALID",
            "Invalid trust_bundle_ref.",
            json!({}),
        );
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    let host = url::Url::parse(&endpoint)
        .ok()
        .and_then(|url| url.host_str().map(str::to_string))
        .unwrap_or_default();
    let evidence = json!({
        "status": "not_checked",
        "source": "redfish",
        "configured_host": host,
    });
    let configured_at = now();
    let sql = if postgres(&state) {
        "UPDATE core_agentmachine SET bmc_endpoint = ?, bmc_credential_ref = ?, \
         bmc_trust_bundle_ref = ?, bmc_tls_verify = TRUE, bmc_configured_at = ?::timestamptz, \
         bmc_identity_evidence = ?::jsonb, updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_agentmachine SET bmc_endpoint = ?, bmc_credential_ref = ?, \
         bmc_trust_bundle_ref = ?, bmc_tls_verify = 1, bmc_configured_at = ?, \
         bmc_identity_evidence = ?, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    let pool = state.identity_pool.as_ref().unwrap();
    if let Err(value) = sqlx::query(sql)
        .bind(&endpoint)
        .bind(credential_ref)
        .bind(trust_ref)
        .bind(&configured_at)
        .bind(evidence.to_string())
        .bind(&configured_at)
        .bind(&machine.id)
        .execute(pool)
        .await
    {
        return db_error("bmc configuration update failed", value);
    }
    audit(
        &state,
        &machine,
        "fleet.machine.bmc_configured",
        "agent_machine",
        &machine.id,
        json!({
            "configured_host": host,
            "tls_verify": true,
            "credential_reference_configured": true,
            "trust_bundle_reference_configured": !trust_ref.is_empty(),
        }),
    )
    .await;
    Json(json!({
        "machine_id": machine.id,
        "bmc_configured": true,
        "endpoint": endpoint,
        "credential_configured": true,
        "trust_bundle_configured": !trust_ref.is_empty(),
        "tls_verify": true,
        "verification_status": "not_checked",
        "network_check_performed": false,
    }))
    .into_response()
}

fn parse_object(body: &Bytes) -> Result<Value, ()> {
    let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(())
    }
}

fn bmc_failure(message: &str, status: StatusCode) -> Response {
    error(status, "BMC_REQUEST_FAILED", message, json!({}))
}

struct BmcClient {
    client: reqwest::Client,
    base: String,
    credential: (String, String),
}

async fn bmc_client(
    endpoint: &str,
    credential_ref: &str,
    trust_ref: &str,
    tenant_id: &str,
    project_id: &str,
) -> Result<BmcClient, Response> {
    let base = crate::fleet_boot::bmc_base(endpoint)
        .map_err(|_| bmc_failure("BMC address is invalid.", StatusCode::BAD_REQUEST))?;
    let secret_json = crate::fleet_boot::vault_secret(credential_ref, tenant_id, project_id)
        .await
        .map_err(|_| {
            bmc_failure(
                "BMC credential reference could not be resolved.",
                StatusCode::BAD_REQUEST,
            )
        })?;
    let secret: Value = serde_json::from_str(&secret_json)
        .map_err(|_| bmc_failure("BMC credential is invalid.", StatusCode::BAD_REQUEST))?;
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
        return Err(bmc_failure(
            "BMC credential is invalid.",
            StatusCode::BAD_REQUEST,
        ));
    }
    let mut builder = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy();
    if !trust_ref.is_empty() {
        let bundle_json = crate::fleet_boot::vault_secret(trust_ref, tenant_id, project_id)
            .await
            .map_err(|_| {
                bmc_failure(
                    "BMC trust bundle could not be resolved.",
                    StatusCode::BAD_REQUEST,
                )
            })?;
        let bundle: Value = serde_json::from_str(&bundle_json)
            .map_err(|_| bmc_failure("BMC trust bundle is invalid.", StatusCode::BAD_REQUEST))?;
        let pem = bundle
            .get("pem")
            .or_else(|| bundle.get("certificate"))
            .or_else(|| bundle.get("tls_ca"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        if pem.is_empty() {
            return Err(bmc_failure(
                "BMC trust bundle is invalid.",
                StatusCode::BAD_REQUEST,
            ));
        }
        let certificate = reqwest::Certificate::from_pem(pem.as_bytes())
            .map_err(|_| bmc_failure("BMC trust bundle is invalid.", StatusCode::BAD_REQUEST))?;
        builder = builder.add_root_certificate(certificate);
    }
    let client = builder
        .build()
        .map_err(|_| bmc_failure("BMC client could not be created.", StatusCode::BAD_GATEWAY))?;
    Ok(BmcClient {
        client,
        base,
        credential: (username, password),
    })
}

async fn redfish_get(bmc: &BmcClient, path: &str) -> Result<Value, Response> {
    if !path.starts_with("/redfish/v1/") || path.len() > 250 || path.contains("..") {
        return Err(bmc_failure(
            "Redfish document path is invalid.",
            StatusCode::BAD_REQUEST,
        ));
    }
    let response = bmc
        .client
        .get(format!("{}{}", bmc.base, path))
        .basic_auth(&bmc.credential.0, Some(&bmc.credential.1))
        .header("accept", "application/json")
        .send()
        .await
        .map_err(|_| {
            bmc_failure(
                "BMC connection or TLS verification failed.",
                StatusCode::BAD_GATEWAY,
            )
        })?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(bmc_failure(
            "BMC authentication failed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    if response.status().is_redirection() {
        return Err(bmc_failure(
            "Redfish redirects are not allowed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    if !response.status().is_success() {
        return Err(bmc_failure(
            "Redfish request failed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    if response.content_length().unwrap_or(0) > 1_048_576 {
        return Err(bmc_failure(
            "Redfish response exceeded the size limit.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if content_type != "application/json" {
        return Err(bmc_failure(
            "Redfish endpoint did not return JSON.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    let bytes = response
        .bytes()
        .await
        .map_err(|_| bmc_failure("Redfish request failed.", StatusCode::BAD_GATEWAY))?;
    if bytes.len() > 1_048_576 {
        return Err(bmc_failure(
            "Redfish response exceeded the size limit.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    serde_json::from_slice(&bytes).map_err(|_| {
        bmc_failure(
            "Redfish endpoint returned invalid JSON.",
            StatusCode::BAD_GATEWAY,
        )
    })
}

fn identity_value(value: Option<&Value>) -> String {
    let text = value
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if matches!(
        text.as_str(),
        "" | "none"
            | "unknown"
            | "default string"
            | "to be filled by o.e.m."
            | "not specified"
            | "not applicable"
            | "00000000-0000-0000-0000-000000000000"
    ) {
        return String::new();
    }
    text.chars().take(256).collect()
}

struct BmcSystem {
    uuid: String,
    serial: String,
    manufacturer: String,
    model: String,
}

async fn redfish_systems(bmc: &BmcClient) -> Result<Vec<BmcSystem>, Response> {
    let collection = redfish_get(bmc, "/redfish/v1/Systems").await?;
    let members = collection
        .get("Members")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            bmc_failure(
                "Redfish Systems collection contains no members.",
                StatusCode::BAD_GATEWAY,
            )
        })?;
    if members.is_empty() || members.len() > 8 {
        return Err(bmc_failure(
            "Redfish Systems collection has an unsupported member count.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    let mut systems = Vec::new();
    for member in members {
        let path = member
            .get("@odata.id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let suffix = path.trim_start_matches("/redfish/v1/Systems/");
        if !path.starts_with("/redfish/v1/Systems/")
            || suffix.is_empty()
            || suffix.len() > 64
            || !suffix.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.')
            })
        {
            return Err(bmc_failure(
                "Redfish Systems member path is invalid.",
                StatusCode::BAD_GATEWAY,
            ));
        }
        let system = redfish_get(bmc, path).await?;
        systems.push(BmcSystem {
            uuid: identity_value(system.get("UUID")),
            serial: identity_value(system.get("SerialNumber")),
            manufacturer: identity_value(system.get("Manufacturer")),
            model: identity_value(system.get("Model")),
        });
    }
    Ok(systems)
}

async fn store_bmc_evidence(
    state: &AppState,
    machine: &Machine,
    evidence: Value,
    identity_status: Option<&str>,
    conflicts: Option<Vec<Value>>,
) -> Result<(), Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    let updated = now();
    let sql = if postgres(state) {
        "UPDATE core_agentmachine SET bmc_identity_evidence = ?::jsonb, \
         identity_status = COALESCE(?, identity_status), \
         identity_conflicts = COALESCE(?::jsonb, identity_conflicts), \
         updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_agentmachine SET bmc_identity_evidence = ?, \
         identity_status = COALESCE(?, identity_status), \
         identity_conflicts = COALESCE(?, identity_conflicts), \
         updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(evidence.to_string())
        .bind(identity_status)
        .bind(conflicts.map(|value| Value::Array(value).to_string()))
        .bind(updated)
        .bind(&machine.id)
        .execute(pool)
        .await
        .map_err(|value| db_error("bmc evidence update failed", value))?;
    Ok(())
}

async fn identity_duplicate(
    state: &AppState,
    machine: &Machine,
    system_uuid: &str,
    serial: &str,
) -> Result<bool, Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    let rows = sqlx::query(
        "SELECT COALESCE(system_uuid, '') AS system_uuid, \
         COALESCE(identity_material, '{}') AS identity_material \
         FROM core_agentmachine WHERE CAST(tenant_id AS TEXT) = ? \
         AND CAST(project_id AS TEXT) = ? AND CAST(id AS TEXT) <> ?",
    )
    .bind(&machine.tenant_id)
    .bind(&machine.project_id)
    .bind(&machine.id)
    .fetch_all(pool)
    .await
    .map_err(|value| db_error("duplicate identity lookup failed", value))?;
    for row in rows {
        let current_uuid = text(&row, "system_uuid").to_lowercase();
        let material = json_value(&text(&row, "identity_material"), json!({}));
        let product_uuid = material
            .get("product_uuid")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let product_serial = material
            .get("product_serial")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        if !system_uuid.is_empty() && (current_uuid == system_uuid || product_uuid == system_uuid) {
            return Ok(true);
        }
        if !serial.is_empty() && product_serial == serial {
            return Ok(true);
        }
    }
    Ok(false)
}

pub async fn verify_bmc(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok((tenant_admin, _, project_editor)) =
        roles(&state, user.id, &tenant_id, &project_id).await
    else {
        return unavailable();
    };
    if !tenant_admin && !project_editor {
        return error(
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
            "Project editor or administrator role is required.",
            json!({}),
        );
    }
    if !body.is_empty() {
        let empty = serde_json::from_slice::<Value>(&body)
            .map(|value| value.is_null() || value.as_object().map(Map::is_empty).unwrap_or(false))
            .unwrap_or(false);
        if !empty {
            return error(
                StatusCode::BAD_REQUEST,
                "BODY_NOT_ACCEPTED",
                "This endpoint does not accept a request body.",
                json!({}),
            );
        }
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    if machine.endpoint.is_empty() || machine.credential_ref.is_empty() {
        return error(
            StatusCode::CONFLICT,
            "BMC_NOT_CONFIGURED",
            "BMC endpoint and credential reference must be configured.",
            json!({}),
        );
    }
    let bmc = match bmc_client(
        &machine.endpoint,
        &machine.credential_ref,
        &machine.trust_ref,
        &machine.tenant_id,
        &machine.project_id,
    )
    .await
    {
        Ok(client) => client,
        Err(response) => {
            let evidence = json!({
                "status": "error",
                "source": "redfish",
                "observed_at": now(),
                "reason": "invalid_response_or_configuration",
            });
            let _ = store_bmc_evidence(&state, &machine, evidence, None, None).await;
            audit(
                &state,
                &machine,
                "fleet.machine.bmc_identity_check_failed",
                "agent_machine",
                &machine.id,
                json!({"verification_status": "error", "reason": "invalid_response_or_configuration"}),
            )
            .await;
            return response;
        }
    };
    let systems = match redfish_systems(&bmc).await {
        Ok(systems) => systems,
        Err(response) => {
            let evidence = json!({
                "status": "error",
                "source": "redfish",
                "observed_at": now(),
                "reason": "redfish_request_failed",
            });
            let _ = store_bmc_evidence(&state, &machine, evidence, None, None).await;
            audit(
                &state,
                &machine,
                "fleet.machine.bmc_identity_check_failed",
                "agent_machine",
                &machine.id,
                json!({"verification_status": "error", "reason": "redfish_request_failed"}),
            )
            .await;
            return response;
        }
    };
    let mut conflicts: Vec<String> = Vec::new();
    let reason;
    let verification_status;
    if machine.identity_status == "conflict" {
        verification_status = "conflict";
        reason = "existing_identity_conflict_requires_review";
    } else if systems.len() != 1 {
        verification_status = "conflict";
        reason = "expected_exactly_one_system";
    } else {
        let system = &systems[0];
        let material_uuid = machine
            .identity_material
            .get("product_uuid")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        let dmi_uuid = if !machine.system_uuid.is_empty() {
            machine.system_uuid.to_lowercase()
        } else {
            material_uuid
        };
        let dmi_serial = machine
            .identity_material
            .get("product_serial")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase();
        if !dmi_uuid.is_empty() && !system.uuid.is_empty() && dmi_uuid != system.uuid {
            conflicts.push("system_uuid".into());
        }
        if !dmi_serial.is_empty() && !system.serial.is_empty() && dmi_serial != system.serial {
            conflicts.push("product_serial".into());
        }
        if !conflicts.is_empty() {
            verification_status = "conflict";
            reason = "dmi_redfish_mismatch";
        } else if dmi_uuid.is_empty()
            || dmi_serial.is_empty()
            || system.uuid.is_empty()
            || system.serial.is_empty()
        {
            verification_status = "candidate";
            reason = "insufficient_identity_material";
        } else if identity_duplicate(&state, &machine, &system.uuid, &system.serial)
            .await
            .unwrap_or(false)
        {
            conflicts.push("system_uuid".into());
            conflicts.push("product_serial".into());
            verification_status = "conflict";
            reason = "duplicate_machine_identity";
        } else {
            verification_status = "verified";
            reason = "dmi_redfish_match";
        }
    }
    let observed = now();
    let system_value = systems
        .first()
        .map(|system| {
            json!({
                "uuid": system.uuid,
                "serial": system.serial,
                "manufacturer": system.manufacturer,
                "model": system.model,
            })
        })
        .unwrap_or_else(|| json!({}));
    let evidence = json!({
        "status": verification_status,
        "source": "redfish_tls_verified",
        "observed_at": observed,
        "reason": reason,
        "system": system_value,
        "conflicts": conflicts,
    });
    let identity_status = if verification_status == "verified" {
        Some("verified")
    } else if verification_status == "conflict" {
        Some("conflict")
    } else {
        None
    };
    let mut appended = machine.identity_conflicts.clone();
    if verification_status == "conflict" {
        appended.push(json!({
            "type": "bmc_identity_conflict",
            "source": "redfish",
            "fields": conflicts,
            "observed_at": observed,
        }));
    }
    if let Err(response) = store_bmc_evidence(
        &state,
        &machine,
        evidence,
        identity_status,
        if verification_status == "conflict" {
            Some(appended)
        } else {
            None
        },
    )
    .await
    {
        return response;
    }
    audit(
        &state,
        &machine,
        "fleet.machine.bmc_identity_checked",
        "agent_machine",
        &machine.id,
        json!({
            "verification_status": verification_status,
            "reason": reason,
            "conflict_fields": conflicts,
        }),
    )
    .await;
    Json(json!({
        "machine_id": machine.id,
        "verification_status": verification_status,
        "identity_status": identity_status.unwrap_or(&machine.identity_status),
        "reason": reason,
        "conflicts": conflicts,
        "system": system_value,
    }))
    .into_response()
}

fn allowed_targets(state: &str) -> &'static [&'static str] {
    match state {
        "discovered" => &["identity_review"],
        "identity_review" => &["commissioned", "discovered"],
        "commissioned" => &["retired"],
        "retired" => &["discovered"],
        _ => &[],
    }
}

pub async fn lifecycle_review(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok((tenant_admin, _, project_editor)) =
        roles(&state, user.id, &tenant_id, &project_id).await
    else {
        return unavailable();
    };
    if !tenant_admin && !project_editor {
        return error(
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
            "Project editor or administrator role is required.",
            json!({}),
        );
    }
    let payload = match parse_object(&body) {
        Ok(value) => value,
        Err(_) => {
            return error(
                StatusCode::BAD_REQUEST,
                "INVALID_BODY",
                "Request body must be a JSON object.",
                json!({}),
            )
        }
    };
    let expected = payload
        .get("expected_state")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let target = payload
        .get("target_state")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if expected.is_empty() || target.is_empty() {
        return error(
            StatusCode::BAD_REQUEST,
            "LIFECYCLE_STATE_REQUIRED",
            "expected_state and target_state are required strings.",
            json!({}),
        );
    }
    let reason = payload
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if !(REASON_MIN..=REASON_MAX).contains(&reason.chars().count()) {
        return error(
            StatusCode::BAD_REQUEST,
            "LIFECYCLE_REASON_INVALID",
            "reason must contain 10 to 500 characters.",
            json!({}),
        );
    }
    if !allowed_targets(expected).contains(&target) {
        return error(
            StatusCode::CONFLICT,
            "LIFECYCLE_TRANSITION_NOT_ENABLED",
            "This lifecycle transition is not enabled.",
            json!({}),
        );
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    if machine.lifecycle_state != expected {
        return error(
            StatusCode::CONFLICT,
            "LIFECYCLE_STATE_CHANGED",
            "Machine lifecycle state changed.",
            json!({"current_state": machine.lifecycle_state}),
        );
    }
    let pool = state.identity_pool.as_ref().unwrap();
    let updated = now();
    let sql = if postgres(&state) {
        "UPDATE core_agentmachine SET lifecycle_state = ?, updated_at = ?::timestamptz \
         WHERE CAST(id AS TEXT) = ? AND lifecycle_state = ?"
    } else {
        "UPDATE core_agentmachine SET lifecycle_state = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ? AND lifecycle_state = ?"
    };
    let result = sqlx::query(sql)
        .bind(target)
        .bind(updated)
        .bind(&machine.id)
        .bind(expected)
        .execute(pool)
        .await;
    match result {
        Ok(result) if result.rows_affected() == 0 => {
            return error(
                StatusCode::CONFLICT,
                "LIFECYCLE_STATE_CHANGED",
                "Machine lifecycle state changed.",
                json!({}),
            )
        }
        Ok(_) => {}
        Err(value) => return db_error("lifecycle update failed", value),
    }
    audit(
        &state,
        &machine,
        "fleet.machine.lifecycle_reviewed",
        "agent_machine",
        &machine.id,
        json!({
            "expected_state": expected,
            "target_state": target,
            "reason": reason,
        }),
    )
    .await;
    Json(json!({
        "machine_id": machine.id,
        "lifecycle_state": target,
        "previous_state": expected,
        "reason": reason,
    }))
    .into_response()
}

#[derive(Deserialize, Default)]
pub struct HostRoleQuery {
    host_role: Option<String>,
}

fn nic_name(value: &Value) -> String {
    for key in ["name", "interface", "device", "id"] {
        if let Some(text) = value.get(key).and_then(Value::as_str) {
            if !text.trim().is_empty() {
                return text.trim().to_string();
            }
        }
    }
    String::new()
}

fn observed_nics(inventory: &Value) -> BTreeMap<String, Value> {
    let mut index = BTreeMap::new();
    for key in ["nics", "network_interfaces", "interfaces"] {
        if let Some(items) = inventory.get(key).and_then(Value::as_array) {
            for item in items {
                if !item.is_object() {
                    continue;
                }
                let name = nic_name(item);
                if !name.is_empty() {
                    index.insert(name, item.clone());
                }
            }
        }
    }
    index
}

fn finding(
    code: &str,
    status: &str,
    network: Option<&str>,
    interface: Option<&str>,
    expected: Value,
    observed: Value,
) -> Value {
    json!({
        "code": code,
        "status": status,
        "network": network,
        "interface": interface,
        "expected": expected,
        "observed": observed,
    })
}

fn nic_vlans(nic: &Value) -> Option<Vec<i64>> {
    for key in ["vlans", "vlan_ids"] {
        if let Some(items) = nic.get(key).and_then(Value::as_array) {
            return Some(items.iter().filter_map(Value::as_i64).collect());
        }
    }
    nic.get("vlan")
        .and_then(Value::as_i64)
        .map(|vlan| vec![vlan])
}

fn verify_host_networks(plan: &Value, host_role: &str, inventory: &Value) -> Result<Value, String> {
    if !HOST_ROLES.contains(&host_role) {
        return Err("INFRA_NETWORK_HOST_ROLE_INVALID".into());
    }
    let networks = plan
        .get("networks")
        .and_then(Value::as_array)
        .ok_or_else(|| "INFRA_NETWORK_PROFILE_INVALID".to_string())?;
    let index = observed_nics(inventory);
    let observed_bonds = inventory
        .get("bonds")
        .or_else(|| inventory.get("bond_interfaces"))
        .and_then(Value::as_array)
        .cloned();
    let mut findings = Vec::new();
    let mut checked = Vec::new();
    for network in networks {
        let role = network
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let name = network
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let enabled = network
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let bound = network
            .get("host_roles")
            .and_then(Value::as_array)
            .map(|roles| roles.iter().any(|value| value.as_str() == Some(host_role)))
            .unwrap_or(false);
        if !bound || !enabled {
            continue;
        }
        checked.push(name.to_string());
        let bond = network.get("bond").filter(|value| value.is_object());
        let link = if let Some(bond) = bond {
            bond.get("name")
                .and_then(Value::as_str)
                .unwrap_or(name)
                .to_string()
        } else {
            network
                .get("interface")
                .and_then(Value::as_str)
                .unwrap_or(name)
                .to_string()
        };
        let interfaces: Vec<String> = if let Some(bond) = bond {
            bond.get("interfaces")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default()
        } else {
            vec![network
                .get("interface")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()]
        };
        let missing: Vec<String> = interfaces
            .iter()
            .filter(|interface| !index.contains_key(*interface))
            .cloned()
            .collect();
        if !missing.is_empty() {
            findings.push(finding(
                "HOST_INTERFACE_MISSING",
                "fail",
                Some(name),
                Some(&link),
                json!(interfaces),
                json!(index.keys().cloned().collect::<Vec<_>>()),
            ));
        }
        if let Some(bond) = bond {
            let bond_name = bond.get("name").and_then(Value::as_str).unwrap_or_default();
            if let Some(observed_bonds) = observed_bonds.as_ref() {
                let match_bond = observed_bonds.iter().find(|item| {
                    nic_name(item) == bond_name
                        || item.get("name").and_then(Value::as_str) == Some(bond_name)
                });
                if match_bond.is_none() {
                    findings.push(finding(
                        "HOST_BOND_MISSING",
                        "fail",
                        Some(name),
                        Some(bond_name),
                        Value::Null,
                        Value::Null,
                    ));
                } else if let Some(match_bond) = match_bond {
                    let observed_mode = match_bond
                        .get("mode")
                        .or_else(|| match_bond.get("bond_mode"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    let expected_mode = bond
                        .get("mode")
                        .and_then(Value::as_str)
                        .unwrap_or("active-backup");
                    if observed_mode.is_empty() {
                        findings.push(finding(
                            "HOST_BOND_MODE_UNKNOWN",
                            "unknown",
                            Some(name),
                            Some(bond_name),
                            json!(expected_mode),
                            Value::Null,
                        ));
                    } else if observed_mode != expected_mode {
                        findings.push(finding(
                            "HOST_BOND_MODE_MISMATCH",
                            "fail",
                            Some(name),
                            Some(bond_name),
                            json!(expected_mode),
                            json!(observed_mode),
                        ));
                    }
                    let members = ["interfaces", "members", "slaves", "ports"]
                        .iter()
                        .find_map(|key| match_bond.get(key).and_then(Value::as_array))
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(Value::as_str)
                                .map(str::to_string)
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default();
                    let mut sorted_members = members.clone();
                    sorted_members.sort();
                    let mut expected_members: Vec<String> = bond
                        .get("interfaces")
                        .and_then(Value::as_array)
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(Value::as_str)
                                .map(str::to_string)
                                .collect()
                        })
                        .unwrap_or_default();
                    expected_members.sort();
                    if members.is_empty() {
                        findings.push(finding(
                            "HOST_BOND_MEMBERS_UNKNOWN",
                            "unknown",
                            Some(name),
                            Some(bond_name),
                            json!(expected_members),
                            Value::Null,
                        ));
                    } else if sorted_members != expected_members {
                        findings.push(finding(
                            "HOST_BOND_MEMBERS_MISMATCH",
                            "fail",
                            Some(name),
                            Some(bond_name),
                            json!(expected_members),
                            json!(members),
                        ));
                    }
                }
            } else {
                findings.push(finding(
                    "HOST_BOND_EVIDENCE_MISSING",
                    "unknown",
                    Some(name),
                    Some(bond_name),
                    bond.clone(),
                    Value::Null,
                ));
            }
        }
        for interface in &interfaces {
            let Some(nic) = index.get(interface) else {
                continue;
            };
            let mtu = nic.get("mtu").and_then(Value::as_i64);
            let expected_mtu = network.get("mtu").and_then(Value::as_i64).unwrap_or(0);
            match mtu {
                None => findings.push(finding(
                    "HOST_MTU_UNKNOWN",
                    "unknown",
                    Some(name),
                    Some(interface),
                    json!(expected_mtu),
                    Value::Null,
                )),
                Some(value) if value != expected_mtu => findings.push(finding(
                    "HOST_MTU_MISMATCH",
                    "fail",
                    Some(name),
                    Some(interface),
                    json!(expected_mtu),
                    json!(value),
                )),
                _ => {}
            }
            if let Some(vlan) = network.get("vlan").and_then(Value::as_i64) {
                match nic_vlans(nic) {
                    None => findings.push(finding(
                        "HOST_VLAN_UNKNOWN",
                        "unknown",
                        Some(name),
                        Some(interface),
                        json!(vlan),
                        Value::Null,
                    )),
                    Some(vlans) if !vlans.contains(&vlan) => findings.push(finding(
                        "HOST_VLAN_MISMATCH",
                        "fail",
                        Some(name),
                        Some(interface),
                        json!(vlan),
                        json!(vlans),
                    )),
                    _ => {}
                }
            }
        }
        let expected_pci: Vec<String> = network
            .get("pci_addresses")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|value| value.to_lowercase())
                    .collect()
            })
            .unwrap_or_default();
        let observed_pci: BTreeSet<String> = index
            .values()
            .filter_map(|nic| nic.get("pci_address").or_else(|| nic.get("pci")))
            .filter_map(Value::as_str)
            .map(|value| value.trim().to_lowercase())
            .filter(|value| !value.is_empty())
            .collect();
        for pci in &expected_pci {
            if observed_pci.is_empty() {
                findings.push(finding(
                    "HOST_PCI_EVIDENCE_MISSING",
                    "unknown",
                    Some(name),
                    Some(&link),
                    json!(pci),
                    Value::Null,
                ));
            } else if !observed_pci.contains(pci) {
                findings.push(finding(
                    "HOST_PCI_MISMATCH",
                    "fail",
                    Some(name),
                    Some(&link),
                    json!(pci),
                    json!(observed_pci),
                ));
            }
        }
        if role == "sriov" && !expected_pci.is_empty() {
            let num_vfs = network.get("num_vfs").and_then(Value::as_i64).unwrap_or(0);
            for nic in index.values() {
                let pci = nic
                    .get("pci_address")
                    .or_else(|| nic.get("pci"))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_lowercase();
                if !expected_pci.contains(&pci) {
                    continue;
                }
                let interface = nic_name(nic);
                if let Some(total) = nic.get("sriov_totalvfs").and_then(Value::as_i64) {
                    if total < num_vfs {
                        findings.push(finding(
                            "HOST_SRIOV_TOTALVFS_INSUFFICIENT",
                            "fail",
                            Some(name),
                            Some(if interface.is_empty() {
                                &link
                            } else {
                                &interface
                            }),
                            json!(num_vfs),
                            json!(total),
                        ));
                    }
                }
                if let Some(allocated) = nic.get("sriov_numvfs").and_then(Value::as_i64) {
                    if allocated != num_vfs {
                        findings.push(finding(
                            "HOST_SRIOV_NUMVFS_MISMATCH",
                            "fail",
                            Some(name),
                            Some(if interface.is_empty() {
                                &link
                            } else {
                                &interface
                            }),
                            json!(num_vfs),
                            json!(allocated),
                        ));
                    }
                } else {
                    findings.push(finding(
                        "HOST_SRIOV_VFS_UNKNOWN",
                        "unknown",
                        Some(name),
                        Some(if interface.is_empty() {
                            &link
                        } else {
                            &interface
                        }),
                        json!(num_vfs),
                        Value::Null,
                    ));
                }
            }
        }
    }
    if checked.is_empty() {
        findings.push(finding(
            "HOST_ROLE_HAS_NO_NETWORK",
            "fail",
            None,
            None,
            Value::Null,
            Value::Null,
        ));
    }
    let status = if findings
        .iter()
        .any(|item| item.get("status").and_then(Value::as_str) == Some("fail"))
    {
        "fail"
    } else if findings
        .iter()
        .any(|item| item.get("status").and_then(Value::as_str) == Some("unknown"))
    {
        "unknown"
    } else {
        "pass"
    };
    let observed_interfaces: Vec<String> = index.keys().cloned().collect();
    let observed_nics: Vec<Value> = index
        .values()
        .map(|nic| {
            let mut object = Map::new();
            for key in [
                "name",
                "mtu",
                "vlans",
                "pci_address",
                "pci_vendor_id",
                "pci_device_id",
                "driver",
                "numa_node",
                "sriov_totalvfs",
                "sriov_numvfs",
            ] {
                if let Some(value) = nic.get(key) {
                    if !value.is_null() && value.as_str() != Some("") {
                        object.insert(key.to_string(), value.clone());
                    }
                }
            }
            Value::Object(object)
        })
        .collect();
    Ok(json!({
        "host_role": host_role,
        "status": status,
        "ok": status == "pass",
        "checked_networks": checked,
        "findings": findings,
        "observed_interfaces": observed_interfaces,
        "observed_nics": observed_nics,
    }))
}

pub async fn verify_networks(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
    Query(query): Query<HostRoleQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok((_, _, project_editor)) = roles(&state, user.id, &tenant_id, &project_id).await else {
        return unavailable();
    };
    if !project_editor {
        return error(
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
            "Project editor or admin role required.",
            json!({}),
        );
    }
    let host_role = query.host_role.as_deref().unwrap_or("worker");
    if !HOST_ROLES.contains(&host_role) {
        return error(
            StatusCode::BAD_REQUEST,
            "INFRA_NETWORK_HOST_ROLE_INVALID",
            "Unknown host_role.",
            json!({}),
        );
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    let pool = state.identity_pool.as_ref().unwrap();
    let profile_row = sqlx::query(
        "SELECT COALESCE(profile, '{}') AS profile FROM core_infrastructurenetworkprofile \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&machine.tenant_id)
    .bind(&machine.project_id)
    .fetch_optional(pool)
    .await;
    let profile_row = match profile_row {
        Ok(row) => row,
        Err(value) => return db_error("infrastructure profile lookup failed", value),
    };
    let Some(profile_row) = profile_row else {
        return error(
            StatusCode::CONFLICT,
            "INFRA_NETWORK_PROFILE_NOT_CONFIGURED",
            "Infrastructure network profile is not configured.",
            json!({}),
        );
    };
    let profile = json_value(&text(&profile_row, "profile"), json!({}));
    let plan = match crate::infra_api::plan(&profile) {
        Ok(plan) => plan,
        Err(code) => {
            return error(
                StatusCode::CONFLICT,
                "INFRA_NETWORK_PROFILE_INVALID",
                &format!("Infrastructure network profile is invalid: {code}"),
                json!({"reason": code}),
            )
        }
    };
    let report = match verify_host_networks(&plan, host_role, &machine.hardware_inventory) {
        Ok(report) => report,
        Err(message) => {
            return error(
                StatusCode::BAD_REQUEST,
                "HOST_NETWORK_OBSERVED_INVALID",
                &message,
                json!({}),
            )
        }
    };
    let mut body = report;
    if let Value::Object(ref mut object) = body {
        object.insert("machine_id".into(), json!(machine.id));
        object.insert("machine_name".into(), json!(machine.name));
        object.insert("inventory_source".into(), json!(machine.inventory_source));
        object.insert(
            "inventory_revision".into(),
            json!(machine.inventory_revision),
        );
        object.insert("observed_at".into(), json!(machine.observed_at));
    }
    Json(body).into_response()
}

fn firmware_vendor(machine: &Machine) -> Option<&'static str> {
    let values = format!("{} {}", machine.vendor, machine.product_name).to_lowercase();
    if values.trim().is_empty() {
        return None;
    }
    let tokens: BTreeSet<&str> = values
        .split(|character: char| !character.is_ascii_alphanumeric())
        .collect();
    if values.contains("hewlett") || values.contains("hpe") || tokens.contains("hp") {
        return Some("HPE");
    }
    if values.contains("dell") {
        return Some("Dell");
    }
    if values.contains("inspur") || values.contains("浪潮") {
        return Some("Inspur");
    }
    if values.contains("h3c") || values.contains("新华三") {
        return Some("H3C");
    }
    if values.contains("lenovo") || values.contains("联想") {
        return Some("Lenovo");
    }
    None
}

fn vendor_gate(machine: &Machine) -> Option<Response> {
    if firmware_vendor(machine).is_some() {
        return None;
    }
    Some(error(
        StatusCode::UNPROCESSABLE_ENTITY,
        "FIRMWARE_VENDOR_UNSUPPORTED",
        "Firmware management supports HPE, Dell, Inspur, H3C and Lenovo BMCs only.",
        json!({"supported_vendors": SUPPORTED_VENDORS}),
    ))
}

fn firmware_job_json(row: &sqlx::any::AnyRow) -> Value {
    json!({
        "id": text(row, "id"),
        "machine_id": text(row, "machine_id"),
        "status": text(row, "status"),
        "image_url": text(row, "image_url"),
        "component": text(row, "component"),
        "apply_time": text(row, "apply_time"),
        "targets": json_value(&text(row, "targets"), json!([])),
        "task_state": text(row, "task_state"),
        "message": text(row, "message"),
        "task_monitor": text(row, "task_monitor"),
        "requested_by": text(row, "requested_by"),
        "created_at": text(row, "created_at"),
        "updated_at": text(row, "updated_at"),
        "finished_at": row.try_get::<Option<String>, _>("finished_at").ok().flatten(),
    })
}

async fn firmware_jobs(state: &AppState, machine: &Machine) -> Result<Vec<Value>, Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    let rows = sqlx::query(
        "SELECT CAST(j.id AS TEXT) AS id, CAST(j.machine_id AS TEXT) AS machine_id, j.status, \
         j.image_url, j.component, j.apply_time, COALESCE(j.targets, '[]') AS targets, \
         j.task_state, j.message, j.task_monitor, COALESCE(u.username, '') AS requested_by, \
         CAST(j.created_at AS TEXT) AS created_at, CAST(j.updated_at AS TEXT) AS updated_at, \
         CAST(j.finished_at AS TEXT) AS finished_at \
         FROM core_firmwareupdatejob j LEFT JOIN platform_users u ON u.id = j.requested_by_id \
         WHERE CAST(j.tenant_id AS TEXT) = ? AND CAST(j.machine_id AS TEXT) = ? \
         ORDER BY j.created_at DESC, j.id DESC LIMIT ?",
    )
    .bind(&machine.tenant_id)
    .bind(&machine.id)
    .bind(FIRMWARE_MAX_JOBS)
    .fetch_all(pool)
    .await
    .map_err(|value| db_error("firmware job lookup failed", value))?;
    Ok(rows.iter().map(firmware_job_json).collect())
}

async fn firmware_inventory(bmc: &BmcClient) -> Result<Value, Response> {
    let service = redfish_get(bmc, "/redfish/v1/UpdateService").await?;
    let enabled = service
        .get("ServiceEnabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let state = service
        .get("Status")
        .and_then(|status| status.get("State"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let mut firmware = Vec::new();
    if let Some(collection_path) = service
        .get("FirmwareInventory")
        .and_then(|link| link.get("@odata.id"))
        .and_then(Value::as_str)
    {
        let collection = redfish_get(bmc, collection_path).await?;
        let members = collection
            .get("Members")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if members.len() > 32 {
            return Err(bmc_failure(
                "Redfish firmware inventory exceeds the supported member limit.",
                StatusCode::BAD_GATEWAY,
            ));
        }
        for member in members {
            let path = member
                .get("@odata.id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if path.is_empty() {
                return Err(bmc_failure(
                    "Redfish firmware inventory member path is invalid.",
                    StatusCode::BAD_GATEWAY,
                ));
            }
            let item = redfish_get(bmc, path).await?;
            let status = item
                .get("Status")
                .and_then(|value| value.get("Health"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            firmware.push(json!({
                "id": item.get("@odata.id").and_then(Value::as_str).unwrap_or(path),
                "name": item.get("Name").and_then(Value::as_str).unwrap_or_default(),
                "version": item.get("Version").and_then(Value::as_str).unwrap_or_default(),
                "updateable": item.get("Updateable").and_then(Value::as_bool).unwrap_or(false),
                "status": status,
            }));
        }
    }
    Ok(json!({
        "service_enabled": enabled,
        "service_state": state,
        "firmware": firmware,
    }))
}

pub async fn list_firmware(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    if let Err(response) = roles(&state, user.id, &tenant_id, &project_id).await {
        return response;
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    if let Some(response) = vendor_gate(&machine) {
        return response;
    }
    let jobs = match firmware_jobs(&state, &machine).await {
        Ok(jobs) => jobs,
        Err(response) => return response,
    };
    let configured = !machine.endpoint.is_empty() && !machine.credential_ref.is_empty();
    let base = json!({
        "machine_id": machine.id,
        "bmc_configured": configured,
        "jobs": jobs,
    });
    if !configured {
        let mut result = base;
        result["detail"] = json!("BMC endpoint and credential reference must be configured.");
        return Json(result).into_response();
    }
    let bmc = match bmc_client(
        &machine.endpoint,
        &machine.credential_ref,
        &machine.trust_ref,
        &machine.tenant_id,
        &machine.project_id,
    )
    .await
    {
        Ok(bmc) => bmc,
        Err(response) => return response,
    };
    let inventory = match firmware_inventory(&bmc).await {
        Ok(inventory) => inventory,
        Err(response) => return response,
    };
    let mut result = base;
    if let (Value::Object(ref mut target), Value::Object(source)) = (&mut result, inventory) {
        target.extend(source);
    }
    Json(result).into_response()
}

fn valid_image_url(value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 500 {
        return Err("image_url must be a non-empty HTTPS URL.".into());
    }
    let parsed =
        url::Url::parse(value).map_err(|_| "image_url must be a valid HTTPS URL.".to_string())?;
    if parsed.scheme() != "https" || parsed.host_str().is_none() {
        return Err("image_url must be a non-empty HTTPS URL.".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("image_url must be an HTTPS URL without embedded credentials.".into());
    }
    if parsed.fragment().is_some() {
        return Err("image_url must not contain a fragment.".into());
    }
    Ok(())
}

fn valid_redfish_target(path: &str) -> bool {
    if !path.starts_with("/redfish/v1/") || path.len() > 250 || path.contains("..") {
        return false;
    }
    path.chars().all(|character| {
        character.is_ascii_alphanumeric()
            || matches!(
                character,
                '.' | '_'
                    | '~'
                    | '!'
                    | '$'
                    | '&'
                    | '\''
                    | '('
                    | ')'
                    | '*'
                    | '+'
                    | ','
                    | ';'
                    | '='
                    | ':'
                    | '@'
                    | '%'
                    | '/'
                    | '-'
            )
    })
}

fn action_target(service: &Value, base: &str) -> Result<String, Response> {
    let target = service
        .get("Actions")
        .and_then(|actions| actions.get("#UpdateService.SimpleUpdate"))
        .and_then(|action| action.get("target"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !target.starts_with(&format!("{base}/redfish/v1/")) {
        return Err(bmc_failure(
            "Redfish UpdateService does not advertise a valid SimpleUpdate target.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    Ok(target.to_string())
}

async fn redfish_post_json(
    bmc: &BmcClient,
    url: &str,
    payload: &Value,
) -> Result<reqwest::Response, Response> {
    let response = bmc
        .client
        .post(url)
        .basic_auth(&bmc.credential.0, Some(&bmc.credential.1))
        .header("accept", "application/json")
        .json(payload)
        .send()
        .await
        .map_err(|_| {
            bmc_failure(
                "BMC connection or TLS verification failed.",
                StatusCode::BAD_GATEWAY,
            )
        })?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(bmc_failure(
            "BMC authentication failed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    if response.status().is_redirection() {
        return Err(bmc_failure(
            "Redfish SimpleUpdate redirects are not allowed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    if !matches!(response.status().as_u16(), 200 | 202) {
        return Err(bmc_failure(
            "Redfish SimpleUpdate failed.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    Ok(response)
}

async fn firmware_job_row(
    state: &AppState,
    machine: &Machine,
    job_id: &str,
) -> Result<Option<sqlx::any::AnyRow>, Response> {
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    sqlx::query(
        "SELECT CAST(j.id AS TEXT) AS id, CAST(j.machine_id AS TEXT) AS machine_id, j.status, \
         j.image_url, j.component, j.apply_time, COALESCE(j.targets, '[]') AS targets, \
         j.task_state, j.message, j.task_monitor, COALESCE(u.username, '') AS requested_by, \
         CAST(j.created_at AS TEXT) AS created_at, CAST(j.updated_at AS TEXT) AS updated_at, \
         CAST(j.finished_at AS TEXT) AS finished_at \
         FROM core_firmwareupdatejob j LEFT JOIN platform_users u ON u.id = j.requested_by_id \
         WHERE CAST(j.tenant_id AS TEXT) = ? AND CAST(j.project_id AS TEXT) = ? \
         AND CAST(j.machine_id AS TEXT) = ? AND CAST(j.id AS TEXT) = ?",
    )
    .bind(&machine.tenant_id)
    .bind(&machine.project_id)
    .bind(&machine.id)
    .bind(job_id)
    .fetch_optional(pool)
    .await
    .map_err(|value| db_error("firmware job lookup failed", value))
}

async fn refresh_firmware_job(
    state: &AppState,
    machine: &Machine,
    job: &mut Value,
) -> Result<(), Response> {
    let status = job
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(status, "succeeded" | "failed") {
        return Ok(());
    }
    let monitor = job
        .get("task_monitor")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if monitor.is_empty() {
        return Ok(());
    }
    let bmc = bmc_client(
        &machine.endpoint,
        &machine.credential_ref,
        &machine.trust_ref,
        &machine.tenant_id,
        &machine.project_id,
    )
    .await?;
    if !monitor.starts_with(&format!("{}/redfish/v1/", bmc.base)) {
        return Err(bmc_failure(
            "Firmware task monitor does not match the machine BMC.",
            StatusCode::BAD_GATEWAY,
        ));
    }
    let path = monitor.trim_start_matches(&bmc.base);
    let task = redfish_get(&bmc, path).await?;
    let task_state = task
        .get("TaskState")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let task_status = task
        .get("TaskStatus")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let message = task
        .get("Messages")
        .and_then(Value::as_array)
        .and_then(|messages| messages.first())
        .and_then(|first| first.get("Message"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .chars()
        .take(500)
        .collect::<String>();
    let mut new_status = status.to_string();
    let mut finished_at: Option<String> = None;
    if task_state == "Completed" {
        new_status = "succeeded".into();
        finished_at = Some(now());
    } else if ["Exception", "Cancelled", "Interrupted", "Killed"].contains(&task_state.as_str()) {
        new_status = "failed".into();
        finished_at = Some(now());
    } else if !task_state.is_empty() {
        new_status = "running".into();
    }
    let combined = if task_status.is_empty() {
        message
    } else {
        format!("{}: {}", task_status, message).trim().to_string()
    };
    let task_state: String = task_state.chars().take(64).collect();
    let pool = state.identity_pool.as_ref().unwrap();
    let updated = now();
    let sql = if postgres(state) {
        "UPDATE core_firmwareupdatejob SET status = ?, task_state = ?, message = ?, \
         finished_at = ?::timestamptz, updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_firmwareupdatejob SET status = ?, task_state = ?, message = ?, \
         finished_at = ?, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(&new_status)
        .bind(&task_state)
        .bind(if combined.is_empty() {
            job.get("message")
                .and_then(Value::as_str)
                .unwrap_or_default()
        } else {
            &combined
        })
        .bind(finished_at.as_deref())
        .bind(updated)
        .bind(job.get("id").and_then(Value::as_str).unwrap_or_default())
        .execute(pool)
        .await
        .map_err(|value| db_error("firmware job refresh failed", value))?;
    job["status"] = json!(new_status);
    job["task_state"] = json!(task_state);
    job["message"] = json!(combined);
    job["finished_at"] = json!(finished_at);
    Ok(())
}

pub async fn request_firmware_upgrade(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id)): Path<(String, String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok((tenant_admin, project_admin, _)) =
        roles(&state, user.id, &tenant_id, &project_id).await
    else {
        return unavailable();
    };
    if !tenant_admin && !project_admin {
        return error(
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
            "Tenant or project administrator role is required.",
            json!({}),
        );
    }
    let payload = match parse_object(&body) {
        Ok(value) => value,
        Err(_) => {
            return error(
                StatusCode::BAD_REQUEST,
                "INVALID_BODY",
                "Request body must be a JSON object.",
                json!({}),
            )
        }
    };
    let allowed = [
        "image_url",
        "component",
        "apply_time",
        "targets",
        "confirm",
        "idempotency_key",
    ];
    if let Some(field) = payload.as_object().and_then(|object| {
        object
            .keys()
            .find(|field| !allowed.contains(&field.as_str()))
    }) {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_FIELDS_UNSUPPORTED",
            "Unsupported firmware upgrade fields were supplied.",
            json!({"field": field}),
        );
    }
    if payload.get("confirm").and_then(Value::as_bool) != Some(true) {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_CONFIRM_REQUIRED",
            "Explicit confirm=true is required for firmware upgrades.",
            json!({}),
        );
    }
    let image_url = payload
        .get("image_url")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if let Err(message) = valid_image_url(image_url) {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_IMAGE_URL_INVALID",
            &message,
            json!({}),
        );
    }
    let component = payload
        .get("component")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if component.chars().count() > 160 {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_COMPONENT_INVALID",
            "component must be at most 160 characters.",
            json!({}),
        );
    }
    let apply_time = payload
        .get("apply_time")
        .and_then(Value::as_str)
        .unwrap_or("Immediate");
    if !matches!(
        apply_time,
        "Immediate" | "OnReset" | "AtMaintenanceWindowStart"
    ) {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_APPLY_TIME_INVALID",
            "apply_time is invalid.",
            json!({}),
        );
    }
    let targets = payload
        .get("targets")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if targets.len() > 8
        || targets
            .iter()
            .any(|target| target.as_str().map(valid_redfish_target) != Some(true))
    {
        return error(
            StatusCode::BAD_REQUEST,
            "FIRMWARE_TARGETS_INVALID",
            "targets must be a list of at most 8 valid Redfish paths.",
            json!({}),
        );
    }
    let idempotency_key = headers
        .get("Idempotency-Key")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if !(8..=128).contains(&idempotency_key.len())
        || !idempotency_key.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | ':' | '-')
        })
    {
        return error(
            StatusCode::BAD_REQUEST,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required (8-128 safe characters).",
            json!({}),
        );
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    if let Some(response) = vendor_gate(&machine) {
        return response;
    }
    if machine.endpoint.is_empty() || machine.credential_ref.is_empty() {
        return error(
            StatusCode::CONFLICT,
            "BMC_NOT_CONFIGURED",
            "BMC endpoint and credential reference must be configured.",
            json!({}),
        );
    }
    let pool = state.identity_pool.as_ref().unwrap();
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id FROM core_firmwareupdatejob \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(machine_id AS TEXT) = ? \
         AND idempotency_key = ? LIMIT 1",
    )
    .bind(&machine.tenant_id)
    .bind(&machine.id)
    .bind(idempotency_key)
    .fetch_optional(pool)
    .await;
    if let Ok(Some(row)) = existing {
        let job_id = text(&row, "id");
        if let Ok(Some(job)) = firmware_job_row(&state, &machine, &job_id).await {
            return Json(firmware_job_json(&job)).into_response();
        }
    }
    let bmc = match bmc_client(
        &machine.endpoint,
        &machine.credential_ref,
        &machine.trust_ref,
        &machine.tenant_id,
        &machine.project_id,
    )
    .await
    {
        Ok(bmc) => bmc,
        Err(response) => return response,
    };
    let service = match redfish_get(&bmc, "/redfish/v1/UpdateService").await {
        Ok(service) => service,
        Err(response) => return response,
    };
    if service.get("ServiceEnabled").and_then(Value::as_bool) != Some(true) {
        return error(
            StatusCode::BAD_GATEWAY,
            "FIRMWARE_SERVICE_DISABLED",
            "Redfish UpdateService is disabled.",
            json!({}),
        );
    }
    let target = match action_target(&service, &bmc.base) {
        Ok(target) => target,
        Err(response) => return response,
    };
    let mut update = json!({
        "ImageURI": image_url,
        "TransferProtocol": "HTTPS",
        "@Redfish.OperationApplyTime": apply_time,
    });
    if !targets.is_empty() {
        update["Targets"] = Value::Array(targets.clone());
    }
    let response = match redfish_post_json(&bmc, &target, &update).await {
        Ok(response) => response,
        Err(response) => return response,
    };
    let mut monitor = response
        .headers()
        .get("location")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string();
    if monitor.is_empty() {
        let body = response.json::<Value>().await.unwrap_or(Value::Null);
        monitor = body
            .get("@odata.id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
    }
    if !monitor.starts_with(&format!("{}/redfish/v1/", bmc.base)) {
        return error(
            StatusCode::BAD_GATEWAY,
            "FIRMWARE_TASK_MONITOR_INVALID",
            "Redfish SimpleUpdate did not return a valid task monitor.",
            json!({}),
        );
    }
    let sanitized_url = url::Url::parse(image_url)
        .map(|parsed| {
            let authority = parsed.port().map_or_else(
                || parsed.host_str().unwrap_or_default().to_string(),
                |port| format!("{}:{port}", parsed.host_str().unwrap_or_default()),
            );
            format!("{}://{}{}", parsed.scheme(), authority, parsed.path())
        })
        .unwrap_or_else(|_| image_url.to_string());
    let job_id = uuid::Uuid::new_v4().to_string();
    let created_at = now();
    let insert = if postgres(&state) {
        "INSERT INTO core_firmwareupdatejob (\
         id, image_url, component, apply_time, targets, status, task_monitor, task_state, \
         message, idempotency_key, created_at, updated_at, machine_id, project_id, \
         requested_by_id, tenant_id) \
         VALUES (?::uuid, ?, ?, ?, ?::jsonb, 'submitted', ?, '', ?, ?, ?::timestamptz, \
         ?::timestamptz, ?::uuid, ?::uuid, ?, ?::uuid)"
    } else {
        "INSERT INTO core_firmwareupdatejob (\
         id, image_url, component, apply_time, targets, status, task_monitor, task_state, \
         message, idempotency_key, created_at, updated_at, machine_id, project_id, \
         requested_by_id, tenant_id) \
         VALUES (?, ?, ?, ?, ?, 'submitted', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)"
    };
    let result = sqlx::query(insert)
        .bind(&job_id)
        .bind(&sanitized_url)
        .bind(component)
        .bind(apply_time)
        .bind(json!(targets).to_string())
        .bind(&monitor)
        .bind("Firmware update submitted to the BMC.")
        .bind(idempotency_key)
        .bind(&created_at)
        .bind(&created_at)
        .bind(&machine.id)
        .bind(&machine.project_id)
        .bind(user.id)
        .bind(&machine.tenant_id)
        .execute(pool)
        .await;
    if let Err(value) = result {
        return db_error("firmware job insert failed", value);
    }
    audit(
        &state,
        &machine,
        "fleet.machine.firmware_update_requested",
        "firmware_update_job",
        &job_id,
        json!({
            "machine_id": machine.id,
            "component": component,
            "apply_time": apply_time,
            "target_count": targets.len(),
            "image_host": url::Url::parse(image_url).ok().and_then(|url| url.host_str().map(str::to_string)).unwrap_or_default(),
        }),
    )
    .await;
    let job = firmware_job_row(&state, &machine, &job_id)
        .await
        .ok()
        .flatten()
        .map(|row| firmware_job_json(&row))
        .unwrap_or_else(|| json!({"id": job_id, "status": "submitted"}));
    (StatusCode::ACCEPTED, Json(job)).into_response()
}

pub async fn firmware_upgrade_detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, machine_id, job_id)): Path<(String, String, String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    if let Err(response) = roles(&state, user.id, &tenant_id, &project_id).await {
        return response;
    }
    let machine = match machine(&state, &tenant_id, &project_id, &machine_id).await {
        Ok(machine) => machine,
        Err(response) => return response,
    };
    let row = match firmware_job_row(&state, &machine, &job_id).await {
        Ok(Some(row)) => row,
        Ok(None) => {
            return error(
                StatusCode::NOT_FOUND,
                "FIRMWARE_JOB_NOT_FOUND",
                "Firmware update job not found.",
                json!({}),
            )
        }
        Err(response) => return response,
    };
    let mut job = firmware_job_json(&row);
    if let Err(response) = refresh_firmware_job(&state, &machine, &mut job).await {
        return response;
    }
    Json(job).into_response()
}
