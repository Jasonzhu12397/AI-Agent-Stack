#![allow(clippy::result_large_err)]

//! Native IM group-robot notification endpoints.
//!
//! Webhook URLs and signing secrets are sealed with the same AES-256-GCM
//! envelope as the Django settings API and are never returned by reads.

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine;
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::is_postgres;

const SCOPE: &str = "notification-endpoint";

pub async fn list_endpoints(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = require_member(pool, &tenant_id, user.id).await {
        return response;
    }
    match endpoint_rows(pool, &tenant_id).await {
        Ok(results) => Json(json!({"results": results})).into_response(),
        Err(response) => response,
    }
}

pub async fn create_endpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = require_admin(pool, &tenant_id, user.id).await {
        return response;
    }
    let Json(payload) = match body {
        Ok(body) => body,
        Err(_) => return invalid_endpoint("name and webhook_url are required."),
    };
    let channel = payload
        .get("channel")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !["dingtalk", "wecom", "feishu"].contains(&channel) {
        return error(
            "INVALID_CHANNEL",
            "channel must be dingtalk, wecom or feishu.",
            StatusCode::BAD_REQUEST,
        );
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let url = payload
        .get("webhook_url")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if name.is_empty() || url.is_empty() {
        return invalid_endpoint("name and webhook_url are required.");
    }
    if !url.starts_with("https://") {
        return invalid_endpoint("webhook_url must be an https URL.");
    }
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    let sealed_url = match seal(secret_key, url, SCOPE) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let secret = payload
        .get("secret")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let sealed_secret = match seal(secret_key, secret, SCOPE) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let enabled = payload
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let id = uuid::Uuid::new_v4().simple().to_string();
    let tenant = canonical(&tenant_id);
    let insert = if is_postgres(&state) {
        sqlx::query(
            "INSERT INTO core_notificationendpoint (id, channel, name, sealed_url, sealed_secret, enabled, last_error, created_at, updated_at, tenant_id) \
             VALUES (?, ?, ?, ?, ?, ?, '', NOW(), NOW(), ?)",
        )
    } else {
        sqlx::query(
            "INSERT INTO core_notificationendpoint (id, channel, name, sealed_url, sealed_secret, enabled, last_error, created_at, updated_at, tenant_id) \
             VALUES (?, ?, ?, ?, ?, ?, '', datetime('now'), datetime('now'), ?)",
        )
    };
    if let Err(error) = insert
        .bind(&id)
        .bind(channel)
        .bind(truncate(name, 120))
        .bind(&sealed_url)
        .bind(&sealed_secret)
        .bind(enabled)
        .bind(&tenant)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    audit_endpoint(
        &state,
        pool,
        &tenant,
        Some(user.id),
        "settings.notification.endpoint",
        &id,
        json!({"channel": channel, "name": name}),
    )
    .await;
    match endpoint_by_id(pool, &tenant, &id).await {
        Ok(Some(payload)) => (StatusCode::CREATED, Json(payload)).into_response(),
        Ok(None) => unavailable(),
        Err(response) => response,
    }
}

pub async fn update_endpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, endpoint_id)): Path<(String, String)>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = require_admin(pool, &tenant_id, user.id).await {
        return response;
    }
    let Json(payload) = match body {
        Ok(body) => body,
        Err(_) => return invalid_endpoint("A JSON object is required."),
    };
    let tenant = canonical(&tenant_id);
    let id = canonical(&endpoint_id);
    let mut statements: Vec<(&str, FieldValue)> = Vec::new();
    if let Some(enabled) = payload.get("enabled").and_then(Value::as_bool) {
        statements.push(("enabled", FieldValue::Bool(enabled)));
    }
    if let Some(name) = payload
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())
    {
        statements.push(("name", FieldValue::Text(truncate(name, 120))));
    }
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    if let Some(url) = payload
        .get("webhook_url")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|url| !url.is_empty())
    {
        if !url.starts_with("https://") {
            return invalid_endpoint("webhook_url must be an https URL.");
        }
        match seal(secret_key, url, SCOPE) {
            Ok(sealed) => statements.push(("sealed_url", FieldValue::Text(sealed))),
            Err(response) => return response,
        }
    }
    if let Some(secret) = payload.get("secret").and_then(Value::as_str) {
        match seal(secret_key, secret.trim(), SCOPE) {
            Ok(sealed) => statements.push(("sealed_secret", FieldValue::Text(sealed))),
            Err(response) => return response,
        }
    }
    let now = if is_postgres(&state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    for (column, value) in statements {
        let sql = format!(
            "UPDATE core_notificationendpoint SET {column} = ?, updated_at = {now} \
             WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
        );
        let statement = sqlx::query(&sql);
        let statement = match value {
            FieldValue::Bool(value) => statement.bind(value),
            FieldValue::Text(value) => statement.bind(value),
        };
        if let Err(error) = statement.bind(&id).bind(&tenant).execute(pool).await {
            return database_error(error.to_string());
        }
    }
    match endpoint_by_id(pool, &tenant, &id).await {
        Ok(Some(payload)) => Json(payload).into_response(),
        Ok(None) => not_found(),
        Err(response) => response,
    }
}

pub async fn delete_endpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, endpoint_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = require_admin(pool, &tenant_id, user.id).await {
        return response;
    }
    let tenant = canonical(&tenant_id);
    let id = canonical(&endpoint_id);
    if let Err(error) = sqlx::query(
        "DELETE FROM core_notificationendpoint WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
    )
    .bind(&id)
    .bind(&tenant)
    .execute(pool)
    .await
    {
        return database_error(error.to_string());
    }
    StatusCode::NO_CONTENT.into_response()
}

pub async fn test_endpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, endpoint_id)): Path<(String, String)>,
) -> Response {
    let _user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = require_member(pool, &tenant_id, _user.id).await {
        return response;
    }
    let tenant = canonical(&tenant_id);
    let id = canonical(&endpoint_id);
    let Some(row) = endpoint_secret_row(pool, &tenant, &id).await else {
        return not_found();
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    let url = match unseal(secret_key, &row.0, SCOPE) {
        Ok(url) => url,
        Err(_) => {
            return error(
                "ENDPOINT_UNREADABLE",
                "The stored webhook could not be read.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let secret = unseal(secret_key, &row.1, SCOPE).unwrap_or_default();
    let title = "AI Agent Studio test notification";
    let body = format!("Tenant notification endpoint {id} was invoked by the native Rust API.");
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0);
    let (url, payload) = build_delivery(&row.2, url, secret, title, &body, timestamp);
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
    {
        Ok(client) => client,
        Err(transport_error) => {
            return error(
                "CHANNEL_TRANSPORT_ERROR",
                &transport_error.to_string(),
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let response = client.post(&url).json(&payload).send().await;
    let (ok, message, status) = match response {
        Ok(response) => {
            let status = response.status();
            let decoded: Value = response.json().await.unwrap_or_else(|_| json!({}));
            let code = decoded
                .get("errcode")
                .or_else(|| decoded.get("code"))
                .and_then(Value::as_i64)
                .unwrap_or(0);
            if !status.is_success() {
                (
                    false,
                    format!("upstream {}", status.as_u16()),
                    Some(status.as_u16()),
                )
            } else if code != 0 && code != 200 {
                (
                    false,
                    format!("channel rejected the message: {code}"),
                    Some(status.as_u16()),
                )
            } else {
                (true, String::new(), Some(status.as_u16()))
            }
        }
        Err(error) => (false, format!("transport error: {error}"), None),
    };
    let now = if is_postgres(&state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let sql = if ok {
        format!("UPDATE core_notificationendpoint SET last_sent_at = {now}, last_error = '', updated_at = {now} WHERE CAST(id AS TEXT) = ?")
    } else {
        format!("UPDATE core_notificationendpoint SET last_error = ?, updated_at = {now} WHERE CAST(id AS TEXT) = ?")
    };
    let mut query = sqlx::query(&sql);
    if !ok {
        query = query.bind(truncate(&message, 200));
    }
    let _ = query.bind(&id).execute(pool).await;
    (
        StatusCode::OK,
        Json(json!({"ok": ok, "message": message, "status_code": status, "last_sent_at": if ok { Some(now_text()) } else { None }})),
    )
        .into_response()
}

fn now_text() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6fZ")
        .to_string()
}

fn build_delivery(
    channel: &str,
    url: String,
    secret: String,
    title: &str,
    body: &str,
    timestamp_ms: u128,
) -> (String, Value) {
    let text = if channel == "feishu" {
        format!("{title}\n{body}")
    } else {
        format!("### {title}\n\n{body}")
    };
    match channel {
        "dingtalk" => {
            let mut url = url;
            if !secret.is_empty() {
                let stamp = timestamp_ms.to_string();
                let payload = format!("{stamp}\n{secret}");
                let mut mac =
                    <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes()).expect("HMAC key");
                mac.update(payload.as_bytes());
                let sign =
                    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
                let separator = if url.contains('?') { '&' } else { '?' };
                url = format!("{url}{separator}timestamp={stamp}&sign={sign}");
            }
            (
                url,
                json!({"msgtype": "markdown", "markdown": {"title": title, "text": text}}),
            )
        }
        "feishu" => {
            let stamp = (timestamp_ms / 1000).to_string();
            let mut payload = json!({"msg_type": "text", "content": {"text": text}});
            if !secret.is_empty() {
                let key = format!("{stamp}\n{secret}");
                let mut mac =
                    <Hmac<Sha256> as Mac>::new_from_slice(key.as_bytes()).expect("HMAC key");
                mac.update(b"");
                let sign =
                    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
                payload["timestamp"] = json!(stamp);
                payload["sign"] = json!(sign);
            }
            (url, payload)
        }
        _ => (
            url,
            json!({"msgtype": "markdown", "markdown": {"content": text}}),
        ),
    }
}

async fn endpoint_rows(pool: &AnyPool, tenant_id: &str) -> Result<Vec<Value>, Response> {
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, channel, name, CAST(enabled AS INTEGER) AS enabled, \
         CAST(last_sent_at AS TEXT) AS last_sent, last_error \
         FROM core_notificationendpoint WHERE CAST(tenant_id AS TEXT) = ? ORDER BY channel, name",
    )
    .bind(canonical(tenant_id))
    .fetch_all(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    Ok(rows.into_iter().map(endpoint_payload).collect())
}

async fn endpoint_by_id(
    pool: &AnyPool,
    tenant_id: &str,
    id: &str,
) -> Result<Option<Value>, Response> {
    let row = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, channel, name, CAST(enabled AS INTEGER) AS enabled, \
         CAST(last_sent_at AS TEXT) AS last_sent, last_error \
         FROM core_notificationendpoint WHERE CAST(tenant_id AS TEXT) = ? AND CAST(id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    Ok(row.map(endpoint_payload))
}

async fn endpoint_secret_row(
    pool: &AnyPool,
    tenant_id: &str,
    id: &str,
) -> Option<(String, String, String)> {
    sqlx::query_as::<_, (String, String, String)>(
        "SELECT sealed_url, sealed_secret, channel FROM core_notificationendpoint \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
}

fn endpoint_payload(row: sqlx::any::AnyRow) -> Value {
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "channel": row.try_get::<String, _>("channel").unwrap_or_default(),
        "name": row.try_get::<String, _>("name").unwrap_or_default(),
        "enabled": row.try_get::<i64, _>("enabled").unwrap_or(0) != 0,
        "last_sent_at": row.try_get::<String, _>("last_sent").ok(),
        "last_error": row.try_get::<String, _>("last_error").unwrap_or_default(),
    })
}

fn seal(secret_key: &str, plaintext: &str, scope: &str) -> Result<String, Response> {
    let mut hasher = Sha256::new();
    hasher.update(format!("{scope}:{secret_key}").as_bytes());
    let key = hasher.finalize();
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| unavailable())?;
    let uuid = uuid::Uuid::new_v4();
    let nonce_bytes = &uuid.as_bytes()[..12];
    let nonce = Nonce::from_slice(nonce_bytes);
    let encrypted = cipher
        .encrypt(
            nonce,
            aes_gcm::aead::Payload {
                msg: plaintext.as_bytes(),
                aad: scope.as_bytes(),
            },
        )
        .map_err(|_| unavailable())?;
    let mut sealed = nonce_bytes.to_vec();
    sealed.extend(encrypted);
    Ok(base64::engine::general_purpose::URL_SAFE.encode(sealed))
}

fn unseal(secret_key: &str, sealed: &str, scope: &str) -> Result<String, ()> {
    let mut hasher = Sha256::new();
    hasher.update(format!("{scope}:{secret_key}").as_bytes());
    let key = hasher.finalize();
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| ())?;
    let raw = base64::engine::general_purpose::URL_SAFE
        .decode(sealed)
        .map_err(|_| ())?;
    if raw.len() < 12 {
        return Err(());
    }
    let nonce = Nonce::from_slice(&raw[..12]);
    let plaintext = cipher
        .decrypt(
            nonce,
            aes_gcm::aead::Payload {
                msg: &raw[12..],
                aad: scope.as_bytes(),
            },
        )
        .map_err(|_| ())?;
    String::from_utf8(plaintext).map_err(|_| ())
}

async fn audit_endpoint(
    state: &AppState,
    pool: &AnyPool,
    tenant: &str,
    actor: Option<i64>,
    event_type: &'static str,
    id: &str,
    metadata: Value,
) {
    if let Some(database_url) = state.config.database_url.as_deref() {
        let event = crate::audit::AuditEvent {
            tenant_id: Some(tenant),
            project_id: None,
            event_type,
            resource_type: "notification_endpoint",
            resource_id: id,
            outcome: "success",
            actor_id: actor,
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        };
        let _ = crate::audit::record_event(pool, database_url, event).await;
    }
}

async fn require_member(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<(), Response> {
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(canonical(tenant_id))
    .bind(user_id)
    .fetch_optional(pool)
    .await;
    match role {
        Ok(Some(_)) => Ok(()),
        Ok(None) => Err(not_found()),
        Err(error) => Err(database_error(error.to_string())),
    }
}

async fn require_admin(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<(), Response> {
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(canonical(tenant_id))
    .bind(user_id)
    .fetch_optional(pool)
    .await;
    match role {
        Ok(Some(role)) if role == "owner" || role == "admin" => Ok(()),
        Ok(Some(_)) => Err(error(
            "TENANT_ADMIN_REQUIRED",
            "Tenant owner or admin role is required to manage notification endpoints.",
            StatusCode::FORBIDDEN,
        )),
        Ok(None) => Err(not_found()),
        Err(error) => Err(database_error(error.to_string())),
    }
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
            tracing::error!(%message, "rust notifications could not resolve the session");
            Err(unavailable())
        }
    }
}

enum FieldValue {
    Bool(bool),
    Text(String),
}

fn canonical(value: &str) -> String {
    value.replace('-', "")
}

fn truncate(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn not_found() -> Response {
    error("NOT_FOUND", "Resource not found.", StatusCode::NOT_FOUND)
}

fn invalid_endpoint(message: &str) -> Response {
    error("INVALID_ENDPOINT", message, StatusCode::BAD_REQUEST)
}

fn unavailable() -> Response {
    error(
        "SERVICE_UNAVAILABLE",
        "The settings store is unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
    )
}

fn database_error(message: String) -> Response {
    tracing::error!(%message, "rust notifications database error");
    error(
        "DATABASE_ERROR",
        "The settings store rejected the request.",
        StatusCode::INTERNAL_SERVER_ERROR,
    )
}

fn error(code: &str, message: &str, status: StatusCode) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message}})),
    )
        .into_response()
}
