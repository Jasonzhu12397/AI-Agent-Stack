#![allow(clippy::result_large_err)]

use std::collections::HashSet;
use std::sync::OnceLock;

use aes_gcm::aead::Aead;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine as _;
use chrono::{Duration, SecondsFormat, Utc};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};
use url::Url;

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, tenant_role};

static PRESETS: OnceLock<Value> = OnceLock::new();

fn presets() -> &'static Value {
    PRESETS.get_or_init(|| {
        serde_json::from_str(include_str!("provider_presets.json")).unwrap_or_else(|_| json!({}))
    })
}

fn preset(key: &str) -> Option<&'static Value> {
    presets().get(key)
}

pub async fn list_providers(
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
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    match provider_rows(pool, &state, &tenant_id, user.id, None).await {
        Ok(rows) => {
            let mut providers = Vec::with_capacity(rows.len());
            for row in rows {
                providers.push(match provider_json(pool, &state, &row, user.id).await {
                    Ok(value) => value,
                    Err(error) => return database_error_message(error),
                });
            }
            Json(providers).into_response()
        }
        Err(error) => database_error_message(error),
    }
}

pub async fn create_provider(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let mut name = match required_text(&payload, "name", 120) {
        Ok(name) => name,
        Err(response) => return response,
    };
    let auth_mode = payload
        .get("auth_mode")
        .and_then(Value::as_str)
        .unwrap_or("tenant_secret");
    if !["tenant_secret", "user_account", "platform_pool"].contains(&auth_mode) {
        return validation_error("auth_mode", "Not a valid choice.");
    }
    let account_mode = auth_mode == "user_account";
    if !account_mode && role != "owner" && role != "admin" {
        return legacy_error(
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
            "TENANT_ADMIN_REQUIRED",
        );
    }
    let provider_type = match payload.get("provider_type").and_then(Value::as_str) {
        Some("fake") => "fake",
        Some("openai_compatible") => "openai_compatible",
        Some(_) => return validation_error("provider_type", "Not a valid choice."),
        None => return validation_error("provider_type", "This field is required."),
    };
    let preset_key = payload
        .get("preset_key")
        .and_then(Value::as_str)
        .unwrap_or("");
    let base_url = payload
        .get("base_url")
        .and_then(Value::as_str)
        .unwrap_or("");
    let credential_ref = payload
        .get("credential_ref")
        .and_then(Value::as_str)
        .unwrap_or("");
    if base_url.chars().count() > 500 {
        return validation_error(
            "base_url",
            "Ensure this field has no more than 500 characters.",
        );
    }
    if credential_ref.chars().count() > 200 {
        return validation_error(
            "credential_ref",
            "Ensure this field has no more than 200 characters.",
        );
    }
    let config = match payload.get("config") {
        Some(Value::Object(config)) => config.clone(),
        Some(_) => return validation_error("config", "Config must be a JSON object."),
        None => Map::new(),
    };
    let mut final_type = provider_type;
    let mut final_base = base_url.to_string();
    let final_credential = credential_ref.to_string();
    if account_mode {
        let Some(platform) = preset(preset_key) else {
            return validation_error("preset_key", "Unknown model platform preset.");
        };
        if final_base.is_empty() {
            final_base = platform
                .get("base_url")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
        }
        if !final_credential.is_empty() {
            return validation_error(
                "credential_ref",
                "Account providers never store a tenant credential.",
            );
        }
        if provider_type != "openai_compatible" {
            return validation_error(
                "provider_type",
                "Account providers use the OpenAI-compatible transport.",
            );
        }
        final_type = "openai_compatible";
        let Some(url) = Url::parse(&final_base).ok() else {
            return validation_error(
                "base_url",
                "Account providers call a public HTTPS platform endpoint.",
            );
        };
        if url.scheme() != "https" || url.host_str().is_none() || !url.username().is_empty() {
            return validation_error(
                "base_url",
                "Account providers call a public HTTPS platform endpoint.",
            );
        }
        let expected = platform
            .get("base_url")
            .and_then(Value::as_str)
            .unwrap_or("");
        if final_base.trim_end_matches('/') != expected.trim_end_matches('/') {
            return validation_error(
                "base_url",
                &format!(
                    "The {} preset is fixed at {}.",
                    platform
                        .get("label")
                        .and_then(Value::as_str)
                        .unwrap_or(preset_key),
                    expected
                ),
            );
        }
    } else {
        if !preset_key.is_empty() {
            return validation_error(
                "preset_key",
                "Only account-billed providers carry a platform preset.",
            );
        }
        if contains_secret(&Value::Object(config.clone())) {
            return validation_error(
                "config",
                "Secret material must be stored through credential_ref, not config.",
            );
        }
        if final_type == "fake" {
            if !final_base.is_empty() || !final_credential.is_empty() {
                return detail_error(
                    "Fake providers cannot contain a base URL or credential reference.",
                    StatusCode::BAD_REQUEST,
                );
            }
        } else {
            let Some(url) = Url::parse(&final_base).ok() else {
                return validation_error("base_url", "Enter a valid URL.");
            };
            let host = url.host_str().unwrap_or("").to_lowercase();
            let remote = allowed_hosts("MODEL_PROVIDER_ALLOWED_HOSTS");
            let local = allowed_hosts("LOCAL_MODEL_ALLOWED_HOSTS");
            let local_http = url.scheme() == "http" && local.contains(&host);
            let remote_https = url.scheme() == "https" && remote.contains(&host);
            if host.is_empty() || !(local_http || remote_https) {
                return validation_error(
                    "base_url",
                    "Use HTTPS with MODEL_PROVIDER_ALLOWED_HOSTS for remote providers, or HTTP with LOCAL_MODEL_ALLOWED_HOSTS for local model servers.",
                );
            }
            let bad_port = remote_https && url.port().is_some() && url.port() != Some(443);
            if !url.username().is_empty()
                || url.password().is_some()
                || url.query().is_some()
                || url.fragment().is_some()
                || bad_port
            {
                return validation_error(
                    "base_url",
                    "Provider URL contains unsupported components.",
                );
            }
            let credential_ok = env_reference(&final_credential);
            if local_http && !final_credential.is_empty() && !credential_ok {
                return validation_error(
                    "credential_ref",
                    "Use an environment reference such as env:LOCAL_MODEL_KEY.",
                );
            }
            if remote_https && !credential_ok {
                return validation_error(
                    "credential_ref",
                    "Remote providers require an env: credential reference.",
                );
            }
        }
    }
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_modelprovider WHERE CAST(tenant_id AS TEXT) = ? AND name = ?",
    )
    .bind(&tenant_id)
    .bind(&name)
    .fetch_one(pool)
    .await;
    if matches!(existing, Ok(value) if value > 0) {
        if !account_mode {
            return legacy_error(
                "A model provider with this name already exists.",
                StatusCode::CONFLICT,
                "MODEL_PROVIDER_NAME_TAKEN",
            );
        }
        let username =
            sqlx::query_scalar::<_, String>("SELECT username FROM auth_user WHERE id = ?")
                .bind(user.id)
                .fetch_optional(pool)
                .await
                .ok()
                .flatten()
                .unwrap_or_default();
        let base = format!("{name} ({username})");
        let mut candidate = base.clone();
        let mut suffix = 2;
        loop {
            let taken = sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM core_modelprovider WHERE CAST(tenant_id AS TEXT) = ? AND name = ?",
            )
            .bind(&tenant_id)
            .bind(&candidate)
            .fetch_one(pool)
            .await
            .unwrap_or(0);
            if taken == 0 {
                break;
            }
            candidate = format!("{base} {suffix}");
            suffix += 1;
        }
        name = candidate;
    }
    let provider_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, \
         preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, TRUE, '', ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, \
         preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&provider_id)
        .bind(&tenant_id)
        .bind(user.id)
        .bind(&name)
        .bind(final_type)
        .bind(auth_mode)
        .bind(preset_key)
        .bind(&final_base)
        .bind(&final_credential)
        .bind(Value::Object(config).to_string())
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error.to_string());
    }
    if account_mode {
        if let Err(error) =
            seed_preset_models(&mut transaction, postgres, &provider_id, preset_key).await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error);
        }
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "model.provider.created",
            resource_type: "model_provider",
            resource_id: &provider_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({"name": name, "auth_mode": auth_mode}),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    let rows = match provider_rows(pool, &state, &tenant_id, user.id, Some(&provider_id)).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error),
    };
    let Some(row) = rows.into_iter().next() else {
        return not_found();
    };
    match provider_json(pool, &state, &row, user.id).await {
        Ok(value) => (StatusCode::CREATED, Json(value)).into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn provider_catalog(
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
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let mut platforms = Vec::new();
    if let Some(object) = presets().as_object() {
        for (key, platform) in object {
            platforms.push(json!({
                "key": key,
                "label": platform.get("label").and_then(Value::as_str).unwrap_or(key),
                "base_url": platform.get("base_url").and_then(Value::as_str).unwrap_or(""),
                "console_url": platform.get("console_url").and_then(Value::as_str).unwrap_or(""),
                "models": platform.get("models").cloned().unwrap_or_else(|| json!([])),
                "oauth": platform.get("oauth").map(Value::is_object).unwrap_or(false),
                "platform_env": platform.get("platform_env").and_then(Value::as_str).unwrap_or(""),
                "platform_configured": platform.get("platform_env").and_then(Value::as_str).map(|name| std::env::var(name).map(|value| !value.is_empty()).unwrap_or(false)).unwrap_or(false),
            }));
        }
    }
    Json(platforms).into_response()
}

pub async fn list_configs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, provider_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    match visible_provider(pool, &tenant_id, &provider_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    match config_rows(pool, &state, &provider_id, None).await {
        Ok(rows) => {
            let mut configs = Vec::with_capacity(rows.len());
            for row in &rows {
                configs.push(config_json(row));
            }
            Json(configs).into_response()
        }
        Err(error) => database_error_message(error),
    }
}

pub async fn create_config(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, provider_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return detail_error(
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    if !visible_provider(pool, &tenant_id, &provider_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let provider_tenant = sqlx::query_scalar::<_, String>(
        "SELECT CAST(tenant_id AS TEXT) FROM core_modelprovider WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&provider_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    if provider_tenant.as_deref() != Some(tenant_id.as_str()) {
        return detail_error(
            "Only tenant-owned providers can be modified by tenant admins.",
            StatusCode::FORBIDDEN,
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let model_name = match required_text(&payload, "model_name", 160) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let display_name = match required_text(&payload, "display_name", 160) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let capabilities = match capabilities(&payload) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let context_window = match integer_field(&payload, "context_window", 32768, 1, i64::MAX) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let max_output_tokens =
        match integer_field(&payload, "max_output_tokens", 4096, 1, context_window) {
            Ok(value) => value,
            Err(response) => return response,
        };
    let input_cost = match cost_field(&payload, "input_cost_per_million") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let output_cost = match cost_field(&payload, "output_cost_per_million") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let enabled = payload
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let fallback = match payload.get("fallback_model") {
        Some(Value::Null) | None => None,
        Some(Value::String(id)) => match uuid::Uuid::parse_str(id) {
            Ok(_) => Some(id.clone()),
            Err(_) => return validation_error("fallback_model", "Must be a valid UUID."),
        },
        Some(_) => return validation_error("fallback_model", "Must be a valid UUID."),
    };
    if let Some(fallback_id) = fallback.as_deref() {
        if !fallback_usable(pool, &tenant_id, user.id, None, fallback_id)
            .await
            .unwrap_or(false)
        {
            return detail_error(
                "The fallback must be an enabled visible chat model.",
                StatusCode::BAD_REQUEST,
            );
        }
    }
    let latest = sqlx::query_scalar::<_, i64>(
        "SELECT COALESCE(MAX(version), 0) FROM core_modelconfig \
         WHERE CAST(provider_id AS TEXT) = ? AND model_name = ?",
    )
    .bind(&provider_id)
    .bind(&model_name)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    let config_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, \
         context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, \
         enabled, fallback_model_id, created_at) VALUES (?::uuid, ?::uuid, ?, ?, ?::jsonb, ?, ?, ?, ?, ?, ?, ?::uuid, ?::timestamptz)"
    } else {
        "INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, \
         context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, \
         enabled, fallback_model_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&config_id)
        .bind(&provider_id)
        .bind(&model_name)
        .bind(&display_name)
        .bind(Value::Array(capabilities.clone()).to_string())
        .bind(context_window)
        .bind(max_output_tokens)
        .bind(input_cost)
        .bind(output_cost)
        .bind(latest + 1)
        .bind(if enabled { 1_i64 } else { 0_i64 })
        .bind(fallback.as_deref())
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "model.config.created",
            resource_type: "model_config",
            resource_id: &config_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "provider_id": provider_id,
                "model_name": model_name,
                "version": latest + 1,
            }),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match config_rows(pool, &state, &provider_id, Some(&config_id)).await {
        Ok(rows) => match rows.first() {
            Some(row) => (StatusCode::CREATED, Json(config_json(row))).into_response(),
            None => not_found(),
        },
        Err(error) => database_error_message(error),
    }
}

pub async fn update_config(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, config_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return legacy_error(
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
            "TENANT_ADMIN_REQUIRED",
        );
    }
    let provider_id = visible_config_provider(pool, &tenant_id, &config_id, user.id)
        .await
        .ok()
        .flatten();
    let Some(provider_id) = provider_id else {
        return not_found();
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let fallback = match payload.get("fallback_model") {
        None => None,
        Some(Value::Null) => Some(None),
        Some(Value::String(id)) => match uuid::Uuid::parse_str(id) {
            Ok(_) => Some(Some(id.clone())),
            Err(_) => return validation_error("fallback_model", "Must be a valid UUID."),
        },
        Some(_) => return validation_error("fallback_model", "Must be a valid UUID."),
    };
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    if let Some(fallback) = fallback {
        if let Some(fallback_id) = fallback.as_deref() {
            if !fallback_usable(pool, &tenant_id, user.id, Some(&config_id), fallback_id)
                .await
                .unwrap_or(false)
            {
                let _ = transaction.rollback().await;
                return detail_error(
                    "The fallback must be an enabled chat model in this tenant's visible providers, and must not form a cycle.",
                    StatusCode::BAD_REQUEST,
                );
            }
        }
        let postgres = is_postgres(&state);
        let update = if postgres {
            "UPDATE core_modelconfig SET fallback_model_id = ?::uuid WHERE CAST(id AS TEXT) = ?"
        } else {
            "UPDATE core_modelconfig SET fallback_model_id = ? WHERE CAST(id AS TEXT) = ?"
        };
        if let Err(error) = sqlx::query(update)
            .bind(fallback.as_deref())
            .bind(&config_id)
            .execute(&mut *transaction)
            .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
        if let Err(error) = record_event_in_transaction(
            &mut transaction,
            postgres,
            &AuditEvent {
                tenant_id: Some(&tenant_id),
                project_id: None,
                event_type: "model.config.routing.updated",
                resource_type: "model_config",
                resource_id: &config_id,
                outcome: "success",
                actor_id: Some(user.id),
                request_id: header(&headers, "x-request-id"),
                remote_addr: None,
                user_agent: header(&headers, "user-agent"),
                metadata: json!({"fallback_model_id": fallback}),
            },
        )
        .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error);
        }
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match config_rows(pool, &state, &provider_id, Some(&config_id)).await {
        Ok(rows) => match rows.first() {
            Some(row) => Json(config_json(row)).into_response(),
            None => not_found(),
        },
        Err(error) => database_error_message(error),
    }
}

struct ProviderRow {
    id: String,
    name: String,
    provider_type: String,
    auth_mode: String,
    preset_key: String,
    base_url: String,
    credential_ref: String,
    enabled: bool,
    created_at: Option<String>,
}

async fn provider_rows(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
    provider_id: Option<&str>,
) -> Result<Vec<ProviderRow>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let created = if postgres {
        "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS created"
            .to_string()
    } else {
        "strftime('%Y-%m-%dT%H:%M:%fZ', created_at) AS created".to_string()
    };
    let base = format!(
        "SELECT CAST(id AS TEXT) AS id, name, provider_type, auth_mode, preset_key, base_url, \
         credential_ref, CAST(enabled AS INTEGER) AS enabled_int, {created} FROM core_modelprovider \
         WHERE (CAST(tenant_id AS TEXT) = ? OR tenant_id IS NULL) \
           AND (auth_mode <> 'user_account' OR created_by_id = ?) \
           AND enabled = {true_literal}"
    );
    let rows = if let Some(provider_id) = provider_id {
        let sql = format!("{base} AND CAST(id AS TEXT) = ? ORDER BY name");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(user_id)
            .bind(provider_id)
            .fetch_all(pool)
            .await
    } else {
        let sql = format!("{base} ORDER BY name");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(user_id)
            .fetch_all(pool)
            .await
    }
    .map_err(|error| error.to_string())?;
    rows.iter()
        .map(|row| {
            Ok(ProviderRow {
                id: row
                    .try_get("id")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                name: row
                    .try_get("name")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                provider_type: row
                    .try_get("provider_type")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                auth_mode: row
                    .try_get("auth_mode")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                preset_key: row
                    .try_get("preset_key")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                base_url: row
                    .try_get("base_url")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                credential_ref: row
                    .try_get("credential_ref")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                enabled: row
                    .try_get::<i64, _>("enabled_int")
                    .map_err(|error| error.to_string())?
                    != 0,
                created_at: row.try_get("created").map_err(|error| error.to_string())?,
            })
        })
        .collect()
}

async fn provider_json(
    pool: &AnyPool,
    state: &AppState,
    row: &ProviderRow,
    user_id: i64,
) -> Result<Value, String> {
    let account = if row.auth_mode == "user_account" {
        account_payload(pool, state, &row.id, user_id).await?
    } else {
        Value::Null
    };
    Ok(json!({
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "auth_mode": row.auth_mode,
        "preset_key": row.preset_key,
        "base_url": row.base_url,
        "credential_configured": !row.credential_ref.is_empty(),
        "account": account,
        "enabled": row.enabled,
        "created_at": row.created_at.as_deref().map(normalize_timestamp),
    }))
}

async fn account_payload(
    pool: &AnyPool,
    state: &AppState,
    provider_id: &str,
    user_id: i64,
) -> Result<Value, String> {
    let postgres = is_postgres(state);
    let linked_at = if postgres {
        "to_char(linked_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')".to_string()
    } else {
        "strftime('%Y-%m-%dT%H:%M:%fZ', linked_at)".to_string()
    };
    let sql = format!(
        "SELECT account_label, credential_ref, {linked_at} AS linked_at \
         FROM core_provideraccountlink WHERE user_id = ? AND CAST(provider_id AS TEXT) = ? \
           AND status = 'linked' ORDER BY linked_at DESC LIMIT 1"
    );
    let link = sqlx::query(&sql)
        .bind(user_id)
        .bind(provider_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    let mut payload = match link {
        None => json!({"linked": false}),
        Some(row) => json!({
            "linked": true,
            "account_label": row.try_get::<String, _>("account_label").unwrap_or_default(),
            "linked_at": row.try_get::<Option<String>, _>("linked_at").ok().flatten().as_deref().map(normalize_timestamp),
            "credential_configured": !row.try_get::<String, _>("credential_ref").unwrap_or_default().is_empty(),
        }),
    };
    let limit = free_daily_tokens();
    let used = consumed_today(pool, postgres, provider_id, user_id).await?;
    let remaining = std::cmp::max(0, limit - used);
    payload["free_daily_tokens"] = json!(limit);
    payload["used_today"] = json!(used);
    payload["remaining_today"] = json!(remaining);
    payload["exhausted"] = json!(used >= limit);
    Ok(payload)
}

fn free_daily_tokens() -> i64 {
    std::env::var("BYO_FREE_DAILY_TOKENS")
        .ok()
        .and_then(|value| value.trim().parse::<i64>().ok())
        .map(|value| value.max(0))
        .unwrap_or(50_000)
}

async fn consumed_today(
    pool: &AnyPool,
    postgres: bool,
    provider_id: &str,
    user_id: i64,
) -> Result<i64, String> {
    let now = Utc::now();
    let day_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap();
    let day_start = if postgres {
        format!("{}", day_start.format("%Y-%m-%d %H:%M:%S"))
    } else {
        day_start.format("%Y-%m-%d %H:%M:%S").to_string()
    };
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    let sql = if postgres {
        "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN consumed_tokens \
         WHEN status = 'reserved' AND expires_at > $1::timestamptz THEN reserved_tokens ELSE 0 END), 0) \
         FROM core_provideraccountusage WHERE user_id = $2 AND CAST(provider_id AS TEXT) = $3 \
           AND created_at >= $4::timestamptz"
    } else {
        "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN consumed_tokens \
         WHEN status = 'reserved' AND expires_at > ? THEN reserved_tokens ELSE 0 END), 0) \
         FROM core_provideraccountusage WHERE user_id = ? AND CAST(provider_id AS TEXT) = ? \
           AND created_at >= ?"
    };
    sqlx::query_scalar::<_, i64>(sql)
        .bind(&now_text)
        .bind(user_id)
        .bind(provider_id)
        .bind(&day_start)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())
}

struct ConfigRow {
    id: String,
    provider_id: String,
    model_name: String,
    display_name: String,
    capabilities: Vec<String>,
    context_window: i64,
    max_output_tokens: i64,
    input_cost: f64,
    output_cost: f64,
    version: i64,
    enabled: bool,
    fallback_model: Option<String>,
    fallback_model_name: Option<String>,
    created_at: Option<String>,
}

fn config_json(row: &ConfigRow) -> Value {
    json!({
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "display_name": row.display_name,
        "capabilities": row.capabilities,
        "context_window": row.context_window,
        "max_output_tokens": row.max_output_tokens,
        "input_cost_per_million": row.input_cost,
        "output_cost_per_million": row.output_cost,
        "version": row.version,
        "enabled": row.enabled,
        "fallback_model": row.fallback_model,
        "fallback_model_name": row.fallback_model_name,
        "created_at": row.created_at.as_deref().map(normalize_timestamp),
    })
}

async fn config_rows(
    pool: &AnyPool,
    state: &AppState,
    provider_id: &str,
    config_id: Option<&str>,
) -> Result<Vec<ConfigRow>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let capabilities = if postgres {
        "config.capabilities::text"
    } else {
        "config.capabilities"
    };
    let created = if postgres {
        "to_char(config.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
            .to_string()
    } else {
        "strftime('%Y-%m-%dT%H:%M:%fZ', config.created_at)".to_string()
    };
    let base = format!(
        "SELECT CAST(config.id AS TEXT) AS id, CAST(config.provider_id AS TEXT) AS provider_id, \
         config.model_name, config.display_name, {capabilities} AS capabilities, \
         config.context_window, config.max_output_tokens, config.input_cost_per_million, \
         config.output_cost_per_million, config.version, CAST(config.enabled AS INTEGER) AS enabled_int, \
         CAST(config.fallback_model_id AS TEXT) AS fallback_model, fallback.display_name AS fallback_model_name, \
         {created} AS created \
         FROM core_modelconfig config LEFT JOIN core_modelconfig fallback ON fallback.id = config.fallback_model_id \
         WHERE CAST(config.provider_id AS TEXT) = ? AND config.enabled = {true_literal}"
    );
    let rows = if let Some(config_id) = config_id {
        let sql = format!("{base} AND CAST(config.id AS TEXT) = ?");
        sqlx::query(&sql)
            .bind(provider_id)
            .bind(config_id)
            .fetch_all(pool)
            .await
    } else {
        let sql = format!("{base} ORDER BY config.display_name, config.version DESC");
        sqlx::query(&sql).bind(provider_id).fetch_all(pool).await
    }
    .map_err(|error| error.to_string())?;
    rows.iter()
        .map(|row| {
            let capabilities = row
                .try_get::<Option<String>, _>("capabilities")
                .map_err(|error| error.to_string())?;
            Ok(ConfigRow {
                id: row
                    .try_get("id")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                provider_id: row
                    .try_get("provider_id")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                model_name: row
                    .try_get("model_name")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                display_name: row
                    .try_get("display_name")
                    .map_err(|error: sqlx::Error| error.to_string())?,
                capabilities: parse_string_array(capabilities),
                context_window: row
                    .try_get("context_window")
                    .map_err(|error| error.to_string())?,
                max_output_tokens: row
                    .try_get("max_output_tokens")
                    .map_err(|error| error.to_string())?,
                input_cost: row
                    .try_get("input_cost_per_million")
                    .map_err(|error| error.to_string())?,
                output_cost: row
                    .try_get("output_cost_per_million")
                    .map_err(|error| error.to_string())?,
                version: row.try_get("version").map_err(|error| error.to_string())?,
                enabled: row
                    .try_get::<i64, _>("enabled_int")
                    .map_err(|error| error.to_string())?
                    != 0,
                fallback_model: row
                    .try_get("fallback_model")
                    .map_err(|error| error.to_string())?,
                fallback_model_name: row
                    .try_get("fallback_model_name")
                    .map_err(|error| error.to_string())?,
                created_at: row.try_get("created").map_err(|error| error.to_string())?,
            })
        })
        .collect()
}

async fn visible_provider(
    pool: &AnyPool,
    tenant_id: &str,
    provider_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_modelprovider \
         WHERE CAST(id AS TEXT) = ? AND (CAST(tenant_id AS TEXT) = ? OR tenant_id IS NULL) \
           AND (auth_mode <> 'user_account' OR created_by_id = ?) AND enabled = 1",
    )
    .bind(provider_id)
    .bind(tenant_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(count > 0)
}

async fn visible_config_provider(
    pool: &AnyPool,
    tenant_id: &str,
    config_id: &str,
    user_id: i64,
) -> Result<Option<String>, String> {
    sqlx::query_scalar::<_, String>(
        "SELECT CAST(provider.id AS TEXT) FROM core_modelconfig config \
         JOIN core_modelprovider provider ON provider.id = config.provider_id \
         WHERE CAST(config.id AS TEXT) = ? \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)",
    )
    .bind(config_id)
    .bind(tenant_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())
}

async fn fallback_usable(
    pool: &AnyPool,
    tenant_id: &str,
    user_id: i64,
    model_id: Option<&str>,
    fallback_id: &str,
) -> Result<bool, String> {
    if model_id == Some(fallback_id) {
        return Ok(false);
    }
    let visible = visible_config_provider(pool, tenant_id, fallback_id, user_id)
        .await?
        .is_some();
    if !visible {
        return Ok(false);
    }
    let mut seen: HashSet<String> = HashSet::new();
    if let Some(model_id) = model_id {
        seen.insert(model_id.to_string());
    }
    let mut current = Some(fallback_id.to_string());
    for _ in 0..3 {
        let Some(id) = current else {
            return Ok(true);
        };
        if !seen.insert(id.clone()) {
            return Ok(false);
        }
        let row = sqlx::query(
            "SELECT CAST(enabled AS INTEGER) AS enabled_int, capabilities, CAST(fallback_model_id AS TEXT) AS fallback \
             FROM core_modelconfig WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
        let Some(row) = row else {
            return Ok(false);
        };
        let enabled = row
            .try_get::<i64, _>("enabled_int")
            .map_err(|error| error.to_string())?
            != 0;
        let capabilities = parse_string_array(
            row.try_get::<Option<String>, _>("capabilities")
                .map_err(|error| error.to_string())?,
        );
        if !enabled || !capabilities.iter().any(|value| value == "chat") {
            return Ok(false);
        }
        current = row.try_get("fallback").map_err(|error| error.to_string())?;
    }
    Ok(current.is_none())
}

async fn seed_preset_models(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    postgres: bool,
    provider_id: &str,
    preset_key: &str,
) -> Result<(), String> {
    let Some(platform) = preset(preset_key) else {
        return Ok(());
    };
    let Some(models) = platform.get("models").and_then(Value::as_array) else {
        return Ok(());
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    for model in models {
        let Some(model_name) = model.get("model_name").and_then(Value::as_str) else {
            continue;
        };
        let display_name = model
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap_or(model_name);
        let exists = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_modelconfig WHERE CAST(provider_id AS TEXT) = ? \
             AND model_name = ? AND version = 1",
        )
        .bind(provider_id)
        .bind(model_name)
        .fetch_one(&mut **transaction)
        .await
        .map_err(|error| error.to_string())?;
        if exists > 0 {
            continue;
        }
        let sql = if postgres {
            "INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, \
             context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, \
             enabled, created_at) VALUES (?::uuid, ?::uuid, ?, ?, '[\"chat\", \"tools\"]'::jsonb, \
             32768, 4096, 0, 0, 1, TRUE, ?::timestamptz)"
        } else {
            "INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, \
             context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, \
             enabled, created_at) VALUES (?, ?, ?, ?, '[\"chat\", \"tools\"]', 32768, 4096, 0, 0, 1, 1, ?)"
        };
        sqlx::query(sql)
            .bind(uuid::Uuid::new_v4().to_string())
            .bind(provider_id)
            .bind(model_name)
            .bind(display_name)
            .bind(&now)
            .execute(&mut **transaction)
            .await
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn parse_string_array(value: Option<String>) -> Vec<String> {
    value
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| {
            value.as_array().map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
        })
        .unwrap_or_default()
}

fn capabilities(payload: &Value) -> Result<Vec<Value>, Response> {
    let allowed = ["chat", "json", "tools", "vision", "embeddings"];
    let Some(value) = payload.get("capabilities") else {
        return Ok(Vec::new());
    };
    let Some(items) = value.as_array() else {
        return Err(validation_error(
            "capabilities",
            "Expected a list of items.",
        ));
    };
    if items
        .iter()
        .any(|item| !item.as_str().is_some_and(|name| allowed.contains(&name)))
    {
        return Err(validation_error(
            "capabilities",
            "Capabilities contain unsupported values.",
        ));
    }
    Ok(items.clone())
}

fn integer_field(
    payload: &Value,
    field: &str,
    default: i64,
    min: i64,
    max: i64,
) -> Result<i64, Response> {
    match payload.get(field) {
        None => Ok(default),
        Some(value) => {
            let Some(number) = value.as_i64() else {
                return Err(validation_error(field, "A valid integer is required."));
            };
            if number < min {
                return Err(validation_error(field, "Must be greater than zero."));
            }
            if number > max {
                return Err(validation_error(
                    field,
                    "Cannot exceed the model context window.",
                ));
            }
            Ok(number)
        }
    }
}

fn cost_field(payload: &Value, field: &str) -> Result<f64, Response> {
    match payload.get(field) {
        None => Ok(0.0),
        Some(value) => {
            let Some(number) = value.as_f64() else {
                return Err(validation_error(field, "A valid number is required."));
            };
            if number < 0.0 {
                return Err(validation_error(field, "Cost cannot be negative."));
            }
            Ok(number)
        }
    }
}

fn contains_secret(value: &Value) -> bool {
    const SECRET_NAMES: [&str; 9] = [
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "authorization",
        "bearer_token",
    ];
    match value {
        Value::Object(map) => map.iter().any(|(key, child)| {
            SECRET_NAMES.contains(&key.to_lowercase().as_str()) || contains_secret(child)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        _ => false,
    }
}

fn allowed_hosts(variable: &str) -> HashSet<String> {
    std::env::var(variable)
        .unwrap_or_default()
        .split(',')
        .map(|value| value.trim().to_lowercase())
        .filter(|value| !value.is_empty())
        .collect()
}

fn env_reference(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("env:") else {
        return false;
    };
    !rest.is_empty()
        && rest.len() <= 128
        && rest.chars().enumerate().all(|(index, character)| {
            if index == 0 {
                character.is_ascii_uppercase()
            } else {
                character.is_ascii_uppercase() || character.is_ascii_digit() || character == '_'
            }
        })
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
            tracing::error!(%message, "rust model api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn parse_object(body: &Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "A valid JSON body is required."})),
        )
            .into_response()),
    }
}

fn required_text(payload: &Value, field: &str, max: usize) -> Result<String, Response> {
    match payload.get(field) {
        Some(Value::String(value)) if !value.trim().is_empty() && value.chars().count() <= max => {
            Ok(value.clone())
        }
        Some(Value::String(value)) if value.trim().is_empty() => {
            Err(validation_error(field, "This field may not be blank."))
        }
        Some(Value::String(_)) => Err(validation_error(
            field,
            &format!("Ensure this field has no more than {max} characters."),
        )),
        Some(_) => Err(validation_error(field, "Not a valid string.")),
        None => Err(validation_error(field, "This field is required.")),
    }
}

fn validation_error(field: &str, message: &str) -> Response {
    let details = json!({field: [message]});
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {"code": "INVALID_REQUEST", "message": "Request could not be processed.", "details": details}})),
    )
        .into_response()
}

fn legacy_error(message: &str, status: StatusCode, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn detail_error(message: &str, status: StatusCode) -> Response {
    (status, Json(json!({"detail": message}))).into_response()
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> &'a str {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
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

fn sqlx_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust model api transaction failed");
    unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust model api database operation failed");
    unavailable()
}

const OAUTH_STATE_TTL_SECONDS: i64 = 600;
const OAUTH_VERIFIER_SCOPE: &str = "provider-oauth-verifier";
const MEMBER_SECRET_SCOPE: &str = "member-credential-secret";

fn oauth_error(code: &str, message: &str, status: StatusCode) -> Response {
    (status, Json(json!({"code": code, "detail": message}))).into_response()
}

fn oauth_config(preset_key: &str) -> Option<&'static Value> {
    preset(preset_key)?.get("oauth")
}

fn random_token() -> String {
    let first = uuid::Uuid::new_v4().simple().to_string();
    let second = uuid::Uuid::new_v4().simple().to_string();
    format!("{first}{second}")
}

fn code_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}

fn seal_value(secret_key: &str, plaintext: &str, scope: &str) -> Result<String, String> {
    let mut hasher = Sha256::new();
    hasher.update(format!("{scope}:{secret_key}").as_bytes());
    let key = hasher.finalize();
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| "invalid key".to_string())?;
    let uuid = uuid::Uuid::new_v4();
    let nonce_bytes = &uuid.as_bytes()[..12];
    let encrypted = cipher
        .encrypt(
            Nonce::from_slice(nonce_bytes),
            aes_gcm::aead::Payload {
                msg: plaintext.as_bytes(),
                aad: scope.as_bytes(),
            },
        )
        .map_err(|_| "seal failed".to_string())?;
    let mut sealed = nonce_bytes.to_vec();
    sealed.extend(encrypted);
    Ok(base64::engine::general_purpose::URL_SAFE.encode(sealed))
}

fn unseal_value(secret_key: &str, sealed: &str, scope: &str) -> Result<String, String> {
    let mut hasher = Sha256::new();
    hasher.update(format!("{scope}:{secret_key}").as_bytes());
    let key = hasher.finalize();
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| "invalid key".to_string())?;
    let raw = base64::engine::general_purpose::URL_SAFE
        .decode(sealed)
        .map_err(|_| "invalid sealed value".to_string())?;
    if raw.len() < 12 {
        return Err("invalid sealed value".to_string());
    }
    let plaintext = cipher
        .decrypt(
            Nonce::from_slice(&raw[..12]),
            aes_gcm::aead::Payload {
                msg: &raw[12..],
                aad: scope.as_bytes(),
            },
        )
        .map_err(|_| "unseal failed".to_string())?;
    String::from_utf8(plaintext).map_err(|_| "unseal failed".to_string())
}

/// Issue an authorization URL for a platform that grants credentials itself.
pub async fn oauth_start(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, preset_key)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    let Some(oauth) = oauth_config(&preset_key) else {
        return oauth_error(
            "OAUTH_PRESET_UNSUPPORTED",
            &format!(
                "{preset_key} does not offer an authorization flow; paste an API key instead."
            ),
            StatusCode::BAD_REQUEST,
        );
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let redirect_uri = payload
        .get("redirect_uri")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if !(redirect_uri.starts_with("http://") || redirect_uri.starts_with("https://")) {
        return oauth_error(
            "OAUTH_REDIRECT_INVALID",
            "The callback URL must be an absolute http(s) address.",
            StatusCode::BAD_REQUEST,
        );
    }
    let verifier = random_token();
    let challenge = code_challenge(&verifier);
    let state_token = uuid::Uuid::new_v4().simple().to_string();
    let sealed = match seal_value(secret_key, &verifier, OAUTH_VERIFIER_SCOPE) {
        Ok(sealed) => sealed,
        Err(_) => return unavailable(),
    };
    let now = Utc::now();
    let expires = (now + Duration::seconds(OAUTH_STATE_TTL_SECONDS))
        .to_rfc3339_opts(SecondsFormat::Micros, false);
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    if let Err(error) = sqlx::query(
        "INSERT INTO core_provideroauthstate (id, preset_key, state, sealed_verifier, redirect_uri, \
         expires_at, consumed_at, created_at, updated_at, tenant_id, user_id) \
         VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
    )
    .bind(uuid::Uuid::new_v4().simple().to_string())
    .bind(&preset_key)
    .bind(&state_token)
    .bind(&sealed)
    .bind(&redirect_uri)
    .bind(&expires)
    .bind(&now_text)
    .bind(&now_text)
    .bind(&tenant_id)
    .bind(user.id)
    .execute(pool)
    .await
    {
        return database_error_message(error.to_string());
    }
    let Some(authorize_url) = oauth.get("authorize_url").and_then(Value::as_str) else {
        return oauth_error(
            "OAUTH_PRESET_UNSUPPORTED",
            "The platform preset has no authorization endpoint.",
            StatusCode::BAD_REQUEST,
        );
    };
    let mut url = match Url::parse(authorize_url) {
        Ok(url) => url,
        Err(_) => return unavailable(),
    };
    url.query_pairs_mut()
        .append_pair("callback_url", &redirect_uri)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");
    (
        StatusCode::CREATED,
        Json(json!({"authorization_url": url.to_string(), "state": state_token})),
    )
        .into_response()
}

/// Redeem an authorization code exactly once and record the account link.
pub async fn oauth_exchange(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, preset_key)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    let Some(oauth) = oauth_config(&preset_key) else {
        return oauth_error(
            "OAUTH_PRESET_UNSUPPORTED",
            &format!(
                "{preset_key} does not offer an authorization flow; paste an API key instead."
            ),
            StatusCode::BAD_REQUEST,
        );
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let code = payload
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let state_token = payload
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if code.is_empty() || state_token.is_empty() {
        return oauth_error(
            "OAUTH_CODE_MISSING",
            "The authorization response is missing its code or state.",
            StatusCode::BAD_REQUEST,
        );
    }
    let attempt = match sqlx::query(
        "SELECT sealed_verifier FROM core_provideroauthstate \
         WHERE user_id = ? AND preset_key = ? AND state = ?",
    )
    .bind(user.id)
    .bind(&preset_key)
    .bind(&state_token)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            return oauth_error(
                "OAUTH_STATE_UNKNOWN",
                "This authorization attempt is unknown or already used. Start again.",
                StatusCode::BAD_REQUEST,
            )
        }
        Err(error) => return database_error_message(error.to_string()),
    };
    let consumed = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_provideroauthstate WHERE user_id = ? AND preset_key = ? \
         AND state = ? AND consumed_at IS NOT NULL",
    )
    .bind(user.id)
    .bind(&preset_key)
    .bind(&state_token)
    .fetch_one(pool)
    .await;
    match consumed {
        Ok(0) => {}
        Ok(_) => {
            return oauth_error(
                "OAUTH_STATE_UNKNOWN",
                "This authorization attempt is unknown or already used. Start again.",
                StatusCode::BAD_REQUEST,
            )
        }
        Err(error) => return database_error_message(error.to_string()),
    }
    let expired = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_provideroauthstate WHERE user_id = ? AND preset_key = ? \
         AND state = ? AND expires_at <= CURRENT_TIMESTAMP",
    )
    .bind(user.id)
    .bind(&preset_key)
    .bind(&state_token)
    .fetch_one(pool)
    .await;
    match expired {
        Ok(0) => {}
        Ok(_) => {
            return oauth_error(
                "OAUTH_STATE_EXPIRED",
                "The authorization attempt expired. Start again.",
                StatusCode::BAD_REQUEST,
            )
        }
        Err(error) => return database_error_message(error.to_string()),
    }
    let sealed = attempt
        .try_get::<String, _>("sealed_verifier")
        .unwrap_or_default();
    let verifier = match unseal_value(secret_key, &sealed, OAUTH_VERIFIER_SCOPE) {
        Ok(verifier) => verifier,
        Err(_) => {
            return oauth_error(
                "OAUTH_STATE_INVALID",
                "The authorization attempt could not be read. Start again.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let Some(key_endpoint) = oauth.get("key_endpoint").and_then(Value::as_str) else {
        return oauth_error(
            "OAUTH_PRESET_UNSUPPORTED",
            "The platform preset has no key exchange endpoint.",
            StatusCode::BAD_REQUEST,
        );
    };
    let mut request_body = json!({"code": code, "code_verifier": verifier});
    if let Some(label) = oauth.get("key_label").and_then(Value::as_str) {
        request_body["key_label"] = json!(label);
    }
    let response = state
        .client
        .post(key_endpoint)
        .timeout(std::time::Duration::from_secs(20))
        .json(&request_body)
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(_) => {
            return oauth_error(
                "OAUTH_EXCHANGE_UNREACHABLE",
                "The platform could not be reached to finish the authorization.",
                StatusCode::BAD_GATEWAY,
            )
        }
    };
    if !response.status().is_success() {
        let status = response.status();
        return oauth_error(
            "OAUTH_EXCHANGE_REFUSED",
            &format!(
                "The platform refused the authorization code (HTTP {}). Try again.",
                status.as_u16()
            ),
            StatusCode::BAD_REQUEST,
        );
    }
    let document: Value = match response.json().await {
        Ok(document) => document,
        Err(_) => {
            return oauth_error(
                "OAUTH_KEY_MISSING",
                "The platform did not return a key for this authorization.",
                StatusCode::BAD_GATEWAY,
            )
        }
    };
    let Some(key) = document.get("key").and_then(Value::as_str).map(str::trim) else {
        return oauth_error(
            "OAUTH_KEY_MISSING",
            "The platform did not return a key for this authorization.",
            StatusCode::BAD_GATEWAY,
        );
    };
    if key.is_empty() {
        return oauth_error(
            "OAUTH_KEY_MISSING",
            "The platform did not return a key for this authorization.",
            StatusCode::BAD_GATEWAY,
        );
    }
    let account_label = document
        .get("label")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(120)
        .collect::<String>();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let provider_id = uuid::Uuid::new_v4().simple().to_string();
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error_message(error.to_string()),
    };
    let existing_provider = match sqlx::query_scalar::<_, String>(
        "SELECT CAST(id AS TEXT) FROM core_modelprovider WHERE CAST(tenant_id AS TEXT) = ? \
         AND created_by_id = ? AND preset_key = ? AND auth_mode = 'user_account' LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(user.id)
    .bind(&preset_key)
    .fetch_optional(&mut *transaction)
    .await
    {
        Ok(provider) => provider,
        Err(error) => return database_error_message(error.to_string()),
    };
    let provider_id = match existing_provider {
        Some(provider_id) => provider_id,
        None => {
            let Some(platform) = preset(&preset_key) else {
                return oauth_error(
                    "OAUTH_PRESET_UNSUPPORTED",
                    "The platform preset is no longer available.",
                    StatusCode::BAD_REQUEST,
                );
            };
            let label = platform
                .get("label")
                .and_then(Value::as_str)
                .unwrap_or(&preset_key);
            let base_name = format!("{label} (my account)");
            let mut name = base_name.clone();
            let mut suffix = 2;
            loop {
                let taken = sqlx::query_scalar::<_, i64>(
                    "SELECT COUNT(*) FROM core_modelprovider WHERE CAST(tenant_id AS TEXT) = ? AND name = ?",
                )
                .bind(&tenant_id)
                .bind(&name)
                .fetch_one(&mut *transaction)
                .await;
                match taken {
                    Ok(0) => break,
                    Ok(_) => {
                        name = format!("{base_name} {suffix}");
                        suffix += 1;
                    }
                    Err(error) => return database_error_message(error.to_string()),
                }
            }
            let base_url = platform
                .get("base_url")
                .and_then(Value::as_str)
                .unwrap_or("");
            if let Err(error) = sqlx::query(
                "INSERT INTO core_modelprovider (id, name, provider_type, base_url, credential_ref, \
                 config, enabled, created_at, updated_at, created_by_id, tenant_id, deployment_region, \
                 auth_mode, preset_key) VALUES (?, ?, 'openai_compatible', ?, '', '{}', 1, ?, ?, ?, ?, '', \
                 'user_account', ?)",
            )
            .bind(&provider_id)
            .bind(&name)
            .bind(base_url)
            .bind(&now)
            .bind(&now)
            .bind(user.id)
            .bind(&tenant_id)
            .bind(&preset_key)
            .execute(&mut *transaction)
            .await
            {
                return database_error_message(error.to_string());
            }
            let postgres = is_postgres(&state);
            if let Err(error) =
                seed_preset_models(&mut transaction, postgres, &provider_id, &preset_key).await
            {
                return database_error_message(error);
            }
            provider_id
        }
    };
    let sealed_key = match seal_value(secret_key, key, MEMBER_SECRET_SCOPE) {
        Ok(sealed) => sealed,
        Err(_) => return unavailable(),
    };
    let secret_id = uuid::Uuid::new_v4().simple().to_string();
    if let Err(error) = sqlx::query(
        "INSERT INTO core_membercredentialsecret (id, sealed_value, created_at, updated_at, user_id) \
         VALUES (?, ?, ?, ?, ?)",
    )
    .bind(&secret_id)
    .bind(&sealed_key)
    .bind(&now)
    .bind(&now)
    .bind(user.id)
    .execute(&mut *transaction)
    .await
    {
        return database_error_message(error.to_string());
    }
    let credential_ref = format!("member://{secret_id}");
    let existing_link = match sqlx::query_scalar::<_, String>(
        "SELECT CAST(id AS TEXT) FROM core_provideraccountlink WHERE user_id = ? \
         AND CAST(provider_id AS TEXT) = ? AND status = 'linked' LIMIT 1",
    )
    .bind(user.id)
    .bind(&provider_id)
    .fetch_optional(&mut *transaction)
    .await
    {
        Ok(link) => link,
        Err(error) => return database_error_message(error.to_string()),
    };
    let linked = if let Some(link_id) = existing_link {
        sqlx::query(
            "UPDATE core_provideraccountlink SET credential_ref = ?, account_label = ?, linked_at = ?, \
             updated_at = ? WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&credential_ref)
        .bind(&account_label)
        .bind(&now)
        .bind(&now)
        .bind(&link_id)
        .execute(&mut *transaction)
        .await
    } else {
        sqlx::query(
            "INSERT INTO core_provideraccountlink (id, status, account_label, credential_ref, linked_at, \
             revoked_at, updated_at, provider_id, user_id) VALUES (?, 'linked', ?, ?, ?, NULL, ?, ?, ?)",
        )
        .bind(uuid::Uuid::new_v4().simple().to_string())
        .bind(&account_label)
        .bind(&credential_ref)
        .bind(&now)
        .bind(&now)
        .bind(&provider_id)
        .bind(user.id)
        .execute(&mut *transaction)
        .await
    };
    if let Err(error) = linked {
        return database_error_message(error.to_string());
    }
    if let Err(error) = sqlx::query(
        "UPDATE core_provideroauthstate SET consumed_at = ?, updated_at = ? WHERE state = ?",
    )
    .bind(&now)
    .bind(&now)
    .bind(&state_token)
    .execute(&mut *transaction)
    .await
    {
        return database_error_message(error.to_string());
    }
    if let Err(error) = transaction.commit().await {
        return database_error_message(error.to_string());
    }
    let rows = match provider_rows(pool, &state, &tenant_id, user.id, Some(&provider_id)).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error),
    };
    let Some(row) = rows.first() else {
        return not_found();
    };
    let provider = match provider_json(pool, &state, row, user.id).await {
        Ok(provider) => provider,
        Err(error) => return database_error_message(error),
    };
    (
        StatusCode::CREATED,
        Json(json!({
            "provider": provider,
            "account": {
                "linked": true,
                "account_label": account_label,
                "linked_at": now,
            }
        })),
    )
        .into_response()
}

/// Read the caller's link and free allowance for one provider.
pub async fn get_provider_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, provider_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let rows = match provider_rows(pool, &state, &tenant_id, user.id, Some(&provider_id)).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error),
    };
    let Some(row) = rows.first() else {
        return not_found();
    };
    let mut payload = match account_payload(pool, &state, &provider_id, user.id).await {
        Ok(payload) => payload,
        Err(error) => return database_error_message(error),
    };
    payload["auth_mode"] = json!(row.auth_mode);
    Json(payload).into_response()
}

/// Revoke the caller's own-account link without deleting audit history.
pub async fn delete_provider_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, provider_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let rows = match provider_rows(pool, &state, &tenant_id, user.id, Some(&provider_id)).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error),
    };
    let Some(row) = rows.first() else {
        return not_found();
    };
    if row.auth_mode != "user_account" {
        return legacy_error(
            "This provider is billed to the tenant; account linking does not apply.",
            StatusCode::CONFLICT,
            "PROVIDER_AUTH_MODE_NOT_ACCOUNT",
        );
    }
    let link = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, account_label FROM core_provideraccountlink \
         WHERE user_id = ? AND CAST(provider_id AS TEXT) = ? AND status = 'linked' \
         ORDER BY linked_at DESC LIMIT 1",
    )
    .bind(user.id)
    .bind(&provider_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(link)) => link,
        Ok(None) => {
            return legacy_error(
                "No account of yours is linked to this provider.",
                StatusCode::NOT_FOUND,
                "PROVIDER_ACCOUNT_NOT_LINKED",
            )
        }
        Err(error) => return database_error_message(error.to_string()),
    };
    let link_id = link.try_get::<String, _>("id").unwrap_or_default();
    let account_label = link
        .try_get::<String, _>("account_label")
        .unwrap_or_default();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error_message(error.to_string()),
    };
    if let Err(error) = sqlx::query(
        "UPDATE core_provideraccountlink SET status = 'revoked', revoked_at = ?, \
         credential_ref = '', updated_at = ? WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&now)
    .bind(&now)
    .bind(&link_id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "model.provider.account.unlinked",
            resource_type: "model_provider",
            resource_id: &provider_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({"account_label": account_label}),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error_message(error.to_string());
    }
    let mut payload = match account_payload(pool, &state, &provider_id, user.id).await {
        Ok(payload) => payload,
        Err(error) => return database_error_message(error),
    };
    payload["auth_mode"] = json!(row.auth_mode);
    Json(payload).into_response()
}

fn chat_model_names(document: &Value) -> Vec<String> {
    let items = document
        .get("data")
        .and_then(Value::as_array)
        .or_else(|| document.get("models").and_then(Value::as_array));
    let Some(items) = items else {
        return Vec::new();
    };
    let skipped = [
        "embedding",
        "embed-",
        "image",
        "audio",
        "speech",
        "transcribe",
        "tts",
        "moderation",
        "rerank",
        "realtime",
        "search",
        "davinci",
        "babbage",
        "ada-",
        "curie",
    ];
    let mut names = Vec::new();
    for item in items {
        let Some(name) = item
            .get("id")
            .and_then(Value::as_str)
            .or_else(|| item.get("name").and_then(Value::as_str))
        else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() {
            continue;
        }
        let lowered = name.to_lowercase();
        if skipped.iter().any(|part| lowered.contains(part)) {
            continue;
        }
        if !names.iter().any(|existing| existing == name) {
            names.push(name.to_string());
        }
    }
    names.truncate(60);
    names
}

fn probe_candidates(preset_key: &str, available: &[String]) -> Vec<String> {
    let preset_names: Vec<String> = preset(preset_key)
        .and_then(|value| value.get("models"))
        .and_then(Value::as_array)
        .map(|models| {
            models
                .iter()
                .filter_map(|model| model.get("model_name").and_then(Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let mut candidates = Vec::new();
    if available.is_empty() {
        candidates.extend(preset_names);
    } else {
        candidates.extend(
            preset_names
                .iter()
                .filter(|name| available.iter().any(|listed| listed == *name))
                .cloned(),
        );
        candidates.extend(
            available
                .iter()
                .filter(|name| !preset_names.iter().any(|preset_name| preset_name == *name))
                .cloned(),
        );
    }
    candidates.truncate(8);
    candidates
}

async fn verify_account_credential(
    state: &AppState,
    provider_name: &str,
    base_url: &str,
    preset_key: &str,
    credential: &str,
) -> Result<Value, Response> {
    if credential.is_empty() {
        return Err(legacy_error(
            "A credential reference for your own account is required.",
            StatusCode::BAD_REQUEST,
            "PROVIDER_ACCOUNT_CREDENTIAL_REQUIRED",
        ));
    }
    if credential.chars().any(char::is_whitespace) {
        return Err(legacy_error(
            "The API key contains whitespace; copy it again without spaces or newlines.",
            StatusCode::BAD_REQUEST,
            "PROVIDER_ACCOUNT_CREDENTIAL_INVALID",
        ));
    }
    if credential.contains("://") || credential.starts_with("env:") {
        return Ok(json!({"verified": null, "reason": "reference", "available_models": []}));
    }
    let models_url = format!("{}/models", base_url.trim_end_matches('/'));
    let response = state
        .client
        .get(&models_url)
        .header("Authorization", format!("Bearer {credential}"))
        .header("Accept", "application/json")
        .timeout(std::time::Duration::from_secs(8))
        .send()
        .await;
    let mut available: Vec<String> = Vec::new();
    match response {
        Ok(response) => {
            let status = response.status();
            if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
                return Err(legacy_error(
                    &format!(
                        "{provider_name} rejected this API key (HTTP {}). Copy a fresh key from the platform's API page and paste it again.",
                        status.as_u16()
                    ),
                    StatusCode::BAD_REQUEST,
                    "PROVIDER_ACCOUNT_CREDENTIAL_REJECTED",
                ));
            }
            if !status.is_success()
                && status != StatusCode::NOT_FOUND
                && status != StatusCode::METHOD_NOT_ALLOWED
            {
                return Ok(json!({
                    "verified": false,
                    "reason": format!("http_{}", status.as_u16()),
                    "available_models": [],
                }));
            }
            if status.is_success() {
                if let Ok(bytes) = response.bytes().await {
                    if let Ok(document) = serde_json::from_slice::<Value>(&bytes) {
                        available = chat_model_names(&document);
                    }
                }
            }
        }
        Err(_) => {
            return Ok(json!({"verified": false, "reason": "unreachable", "available_models": []}))
        }
    }
    let candidates = probe_candidates(preset_key, &available);
    if candidates.is_empty() {
        return Ok(json!({
            "verified": false,
            "reason": "chat_unprobed",
            "available_models": available,
        }));
    }
    let completion_url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let mut last_status = None;
    let mut last_detail = String::new();
    for model in candidates {
        let body = json!({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        });
        let response = state
            .client
            .post(&completion_url)
            .header("Authorization", format!("Bearer {credential}"))
            .header("Accept", "application/json")
            .timeout(std::time::Duration::from_secs(10))
            .json(&body)
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(_) => {
                return Ok(json!({
                    "verified": false,
                    "reason": "chat_unreachable",
                    "available_models": available,
                    "model": model,
                }))
            }
        };
        let status = response.status();
        if status.is_success() {
            return Ok(json!({
                "verified": true,
                "reason": "chat_completion",
                "model": model,
                "available_models": available,
            }));
        }
        last_status = Some(status);
        if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
            return Err(legacy_error(
                &format!(
                    "{provider_name} rejected this API key for chat completion (HTTP {}). The key may be restricted to model listing; create a key with chat permission and paste it again.",
                    status.as_u16()
                ),
                StatusCode::BAD_REQUEST,
                "PROVIDER_ACCOUNT_CREDENTIAL_REJECTED",
            ));
        }
        if status == StatusCode::NOT_FOUND || status == StatusCode::BAD_REQUEST {
            last_detail = response
                .text()
                .await
                .unwrap_or_default()
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(300)
                .collect();
            continue;
        }
        if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
            return Ok(json!({
                "verified": false,
                "reason": format!("chat_{}", status.as_u16()),
                "available_models": available,
                "model": model,
            }));
        }
        return Ok(json!({
            "verified": false,
            "reason": format!("chat_{}", status.as_u16()),
            "available_models": available,
            "model": model,
        }));
    }
    let detail = if last_detail.is_empty() {
        String::new()
    } else {
        format!(" Provider said: {last_detail}")
    };
    Err(legacy_error(
        &format!(
            "{provider_name} accepted the API key, but none of the configured models could run a completion (last HTTP {}). The model id or endpoint id is not available to this account. Add the exact id from the provider console in Model Manager, then link the account again.{detail}",
            last_status.map(|status| status.as_u16()).unwrap_or(400)
        ),
        StatusCode::BAD_REQUEST,
        "PROVIDER_ACCOUNT_NO_CHAT_MODEL",
    ))
}

async fn refresh_preset_models(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    postgres: bool,
    provider_id: &str,
    preset_key: &str,
) -> Result<(), String> {
    seed_preset_models(transaction, postgres, provider_id, preset_key).await?;
    let wanted: Vec<String> = preset(preset_key)
        .and_then(|value| value.get("models"))
        .and_then(Value::as_array)
        .map(|models| {
            models
                .iter()
                .filter_map(|model| model.get("model_name").and_then(Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, model_name, capabilities, CAST(enabled AS INTEGER) AS enabled_int, \
         input_cost_per_million, output_cost_per_million FROM core_modelconfig \
         WHERE CAST(provider_id AS TEXT) = ?",
    )
    .bind(provider_id)
    .fetch_all(&mut **transaction)
    .await
    .map_err(|error| error.to_string())?;
    for row in rows {
        let model_name = row.try_get::<String, _>("model_name").unwrap_or_default();
        if wanted.iter().any(|name| name == &model_name) {
            continue;
        }
        if row.try_get::<i64, _>("enabled_int").unwrap_or(0) == 0 {
            continue;
        }
        let capabilities = row.try_get::<String, _>("capabilities").unwrap_or_default();
        if capabilities.replace(' ', "") != "[\"chat\",\"tools\"]" {
            continue;
        }
        let input = row
            .try_get::<f64, _>("input_cost_per_million")
            .unwrap_or(0.0);
        let output = row
            .try_get::<f64, _>("output_cost_per_million")
            .unwrap_or(0.0);
        if input != 0.0 || output != 0.0 {
            continue;
        }
        sqlx::query("UPDATE core_modelconfig SET enabled = 0 WHERE CAST(id AS TEXT) = ?")
            .bind(row.try_get::<String, _>("id").unwrap_or_default())
            .execute(&mut **transaction)
            .await
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

async fn sync_account_models(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    provider_id: &str,
    preset_key: &str,
    wanted: &[String],
) -> Result<(), String> {
    if wanted.is_empty() {
        return Ok(());
    }
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    for name in wanted {
        let exists = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_modelconfig WHERE CAST(provider_id AS TEXT) = ? \
             AND model_name = ? AND version = 1",
        )
        .bind(provider_id)
        .bind(name)
        .fetch_one(&mut **transaction)
        .await
        .map_err(|error| error.to_string())?;
        if exists > 0 {
            continue;
        }
        sqlx::query(
            "INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, \
             context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, \
             enabled, created_at) VALUES (?, ?, ?, ?, '[\"chat\", \"tools\"]', 32768, 4096, 0, 0, 1, 1, ?)",
        )
        .bind(uuid::Uuid::new_v4().simple().to_string())
        .bind(provider_id)
        .bind(name)
        .bind(name)
        .bind(&now)
        .execute(&mut **transaction)
        .await
        .map_err(|error| error.to_string())?;
    }
    let preset_names: Vec<String> = preset(preset_key)
        .and_then(|value| value.get("models"))
        .and_then(Value::as_array)
        .map(|models| {
            models
                .iter()
                .filter_map(|model| model.get("model_name").and_then(Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, model_name, capabilities, CAST(enabled AS INTEGER) AS enabled_int, \
         input_cost_per_million, output_cost_per_million FROM core_modelconfig \
         WHERE CAST(provider_id AS TEXT) = ?",
    )
    .bind(provider_id)
    .fetch_all(&mut **transaction)
    .await
    .map_err(|error| error.to_string())?;
    for row in rows {
        let model_name = row.try_get::<String, _>("model_name").unwrap_or_default();
        if wanted.iter().any(|name| name == &model_name) {
            continue;
        }
        if !preset_names.iter().any(|name| name == &model_name) {
            continue;
        }
        if row.try_get::<i64, _>("enabled_int").unwrap_or(0) == 0 {
            continue;
        }
        let capabilities = row.try_get::<String, _>("capabilities").unwrap_or_default();
        if capabilities.replace(' ', "") != "[\"chat\",\"tools\"]" {
            continue;
        }
        let input = row
            .try_get::<f64, _>("input_cost_per_million")
            .unwrap_or(0.0);
        let output = row
            .try_get::<f64, _>("output_cost_per_million")
            .unwrap_or(0.0);
        if input != 0.0 || output != 0.0 {
            continue;
        }
        sqlx::query("UPDATE core_modelconfig SET enabled = 0 WHERE CAST(id AS TEXT) = ?")
            .bind(row.try_get::<String, _>("id").unwrap_or_default())
            .execute(&mut **transaction)
            .await
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

/// Link the caller's own API key after verifying it can actually run inference.
pub async fn link_provider_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, provider_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let Some(credential) = payload.get("credential_ref").and_then(Value::as_str) else {
        return legacy_error(
            "credential_ref and account_label must be strings.",
            StatusCode::BAD_REQUEST,
            "PROVIDER_ACCOUNT_INPUT_INVALID",
        );
    };
    let Some(label) = payload.get("account_label").and_then(Value::as_str) else {
        return legacy_error(
            "credential_ref and account_label must be strings.",
            StatusCode::BAD_REQUEST,
            "PROVIDER_ACCOUNT_INPUT_INVALID",
        );
    };
    let credential = credential.trim();
    let account_label: String = label.trim().chars().take(120).collect();
    let rows = match provider_rows(pool, &state, &tenant_id, user.id, Some(&provider_id)).await {
        Ok(rows) => rows,
        Err(error) => return database_error_message(error),
    };
    let Some(row) = rows.first() else {
        return not_found();
    };
    if row.auth_mode != "user_account" {
        return legacy_error(
            "This provider is billed to the tenant; account linking does not apply.",
            StatusCode::CONFLICT,
            "PROVIDER_AUTH_MODE_NOT_ACCOUNT",
        );
    }
    let verification = match verify_account_credential(
        &state,
        &row.name,
        &row.base_url,
        &row.preset_key,
        credential,
    )
    .await
    {
        Ok(verification) => verification,
        Err(response) => return response,
    };
    let available_models: Vec<String> = verification
        .get("available_models")
        .and_then(Value::as_array)
        .map(|models| {
            models
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let reference = if credential.contains("://") || credential.starts_with("env:") {
        credential.to_string()
    } else {
        let sealed = match seal_value(secret_key, credential, MEMBER_SECRET_SCOPE) {
            Ok(sealed) => sealed,
            Err(_) => return unavailable(),
        };
        let secret_id = uuid::Uuid::new_v4().simple().to_string();
        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
        if let Err(error) = sqlx::query(
            "INSERT INTO core_membercredentialsecret (id, sealed_value, created_at, updated_at, user_id) \
             VALUES (?, ?, ?, ?, ?)",
        )
        .bind(&secret_id)
        .bind(&sealed)
        .bind(&now)
        .bind(&now)
        .bind(user.id)
        .execute(pool)
        .await
        {
            return database_error_message(error.to_string());
        }
        format!("member://{secret_id}")
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error_message(error.to_string()),
    };
    if let Err(error) =
        refresh_preset_models(&mut transaction, postgres, &provider_id, &row.preset_key).await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if !available_models.is_empty() {
        if let Err(error) = sync_account_models(
            &mut transaction,
            &provider_id,
            &row.preset_key,
            &available_models,
        )
        .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error);
        }
    }
    let existing_link = match sqlx::query_scalar::<_, String>(
        "SELECT CAST(id AS TEXT) FROM core_provideraccountlink WHERE user_id = ? \
         AND CAST(provider_id AS TEXT) = ? AND status = 'linked' LIMIT 1",
    )
    .bind(user.id)
    .bind(&provider_id)
    .fetch_optional(&mut *transaction)
    .await
    {
        Ok(link) => link,
        Err(error) => return database_error_message(error.to_string()),
    };
    let linked = if let Some(link_id) = existing_link {
        sqlx::query(
            "UPDATE core_provideraccountlink SET credential_ref = ?, account_label = ?, linked_at = ?, \
             updated_at = ? WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&reference)
        .bind(&account_label)
        .bind(&now)
        .bind(&now)
        .bind(&link_id)
        .execute(&mut *transaction)
        .await
    } else {
        sqlx::query(
            "INSERT INTO core_provideraccountlink (id, status, account_label, credential_ref, linked_at, \
             revoked_at, updated_at, provider_id, user_id) VALUES (?, 'linked', ?, ?, ?, NULL, ?, ?, ?)",
        )
        .bind(uuid::Uuid::new_v4().simple().to_string())
        .bind(&account_label)
        .bind(&reference)
        .bind(&now)
        .bind(&now)
        .bind(&provider_id)
        .bind(user.id)
        .execute(&mut *transaction)
        .await
    };
    if let Err(error) = linked {
        let _ = transaction.rollback().await;
        return database_error_message(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "model.provider.account.linked",
            resource_type: "model_provider",
            resource_id: &provider_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({"account_label": account_label, "auth_mode": row.auth_mode}),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error_message(error.to_string());
    }
    let mut account = match account_payload(pool, &state, &provider_id, user.id).await {
        Ok(account) => account,
        Err(error) => return database_error_message(error),
    };
    account["verification"] = verification;
    (StatusCode::CREATED, Json(account)).into_response()
}
