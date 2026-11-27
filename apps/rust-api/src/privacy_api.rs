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
    tracing::error!(%value, "rust privacy api database operation failed");
    unavailable()
}

fn timestamp_value(row: &sqlx::any::AnyRow, column: &str) -> Value {
    let text: Option<String> = row.try_get(column).ok().flatten();
    text.map(|value| json!(value)).unwrap_or(Value::Null)
}

/// Native replacement for `GET /api/v1/tenants/{tenant}/privacy/grants/`.
pub async fn list_grants(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error("Not found.", 404);
    }
    let granted = if is_postgres(&state) {
        "to_char(i.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS granted_at"
    } else {
        "strftime('%Y-%m-%d %H:%M:%S', i.created_at) AS granted_at"
    };
    let sql = format!(
        "SELECT CAST(i.id AS TEXT) AS id, CAST(i.project_id AS TEXT) AS project_id, \
         CAST(i.granted_capabilities AS TEXT) AS scopes_text, i.status, {granted}, \
         p.name AS connector_name, p.plugin_key AS connector_slug \
         FROM core_agentplugininstallation i JOIN core_agentplugin p ON p.id = i.plugin_id \
         WHERE CAST(i.tenant_id AS TEXT) = ? ORDER BY i.created_at DESC"
    );
    let rows = match sqlx::query(&sql).bind(&tenant_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let id: String = row.try_get("id").unwrap_or_default();
            json!({
                "id": id.clone(),
                "source": "agent_connector",
                "installation_id": id,
                "connector_name": row.try_get::<String, _>("connector_name").unwrap_or_default(),
                "connector_slug": row.try_get::<String, _>("connector_slug").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "scopes": row
                    .try_get::<String, _>("scopes_text")
                    .ok()
                    .and_then(|value| serde_json::from_str::<Value>(&value).ok())
                    .unwrap_or_else(|| json!([])),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "granted_at": timestamp_value(row, "granted_at"),
            })
        })
        .collect();
    Json(json!({"results": results})).into_response()
}

/// Native replacement for `DELETE /api/v1/tenants/{tenant}/privacy/grants/{grant}/`.
pub async fn revoke_grant(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, grant_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error("Not found.", 404);
    }
    match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return error("Tenant admin permission is required.", 403),
        Err(value) => return database_error(value),
    }
    let project_id: Option<String> = match sqlx::query_scalar::<_, String>(
        "SELECT CAST(project_id AS TEXT) FROM core_agentplugininstallation \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&grant_id)
    .bind(&tenant_id)
    .fetch_optional(pool)
    .await
    {
        Ok(value) => value,
        Err(value) => return database_error(value.to_string()),
    };
    let Some(project_id) = project_id else {
        return error("Grant not found for this tenant.", 404);
    };
    let status: String = sqlx::query_scalar(
        "SELECT status FROM core_agentplugininstallation WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&grant_id)
    .fetch_one(pool)
    .await
    .unwrap_or_default();
    let already = status == "disabled";
    if !already {
        let now = timestamp();
        let now_expr = if is_postgres(&state) {
            "NOW()".to_string()
        } else {
            format!("'{now}'")
        };
        let sql = format!(
            "UPDATE core_agentplugininstallation SET status = 'disabled', updated_at = {now_expr} \
             WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
        );
        if let Err(value) = sqlx::query(&sql)
            .bind(&grant_id)
            .bind(&tenant_id)
            .execute(pool)
            .await
        {
            return database_error(value.to_string());
        }
        let metadata = json!({"grant_id": grant_id, "already_revoked": already});
        let _ = crate::audit::record_event(
            pool,
            state.config.database_url.as_deref().unwrap_or(""),
            crate::audit::AuditEvent {
                tenant_id: Some(tenant_id.as_str()),
                project_id: Some(project_id.as_str()),
                event_type: "privacy.grant.revoked",
                resource_type: "agent_plugin_installation",
                resource_id: grant_id.as_str(),
                outcome: "success",
                actor_id: Some(user.id),
                request_id: "",
                remote_addr: None,
                user_agent: "",
                metadata,
            },
        )
        .await;
    }
    Json(json!({"revoked": true, "already_revoked": already})).into_response()
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

/// Native replacement for `GET|POST /api/v1/tenants/{tenant}/privacy/deletion-requests/`.
pub async fn deletion_requests(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
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
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error("Not found.", 404);
    }
    if method == axum::http::Method::GET {
        return list_deletion_requests(&state, pool, &tenant_id).await;
    }
    let admin = match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(value) => return database_error(value),
    };
    if !admin {
        return error("Tenant admin permission is required.", 403);
    }
    let body = payload
        .map(|Json(value)| value)
        .unwrap_or_else(|| json!({}));
    let scope = body.get("scope").and_then(Value::as_str).unwrap_or("");
    if !matches!(scope, "archived_content" | "exports") {
        return error("scope must be one of: archived_content, exports.", 400);
    }
    if body.get("confirm") != Some(&Value::Bool(true)) {
        return error("confirm must be true to request deletion.", 400);
    }
    let key = body
        .get("idempotency_key")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if key.is_empty() {
        return error("An idempotency_key is required.", 400);
    }
    if let Some(existing) = find_deletion_request(pool, &tenant_id, &key).await {
        return Json(existing).into_response();
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let now = timestamp();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let metadata = json!({"scope": scope});
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: None,
            event_type: "privacy.deletion_request.created",
            resource_type: "deletion_request",
            resource_id: id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        },
    )
    .await;
    let sql = format!(
        "INSERT INTO core_deletionrequest (id, tenant_id, project_id, scope, status, requested_by_id, \
         idempotency_key, audit_ref, created_at, updated_at) \
         VALUES (?, ?, NULL, ?, 'requested', ?, ?, '', {now_expr}, {now_expr})"
    );
    if let Err(value) = sqlx::query(&sql)
        .bind(&id)
        .bind(&tenant_id)
        .bind(json!({"scope": scope}).to_string())
        .bind(user.id)
        .bind(&key)
        .execute(pool)
        .await
    {
        if let Some(existing) = find_deletion_request(pool, &tenant_id, &key).await {
            return Json(existing).into_response();
        }
        return database_error(value.to_string());
    }
    (
        StatusCode::CREATED,
        Json(json!({
            "id": id,
            "scope": scope,
            "status": "requested",
            "audit_ref": "",
            "executed_at": null,
        })),
    )
        .into_response()
}

async fn find_deletion_request(pool: &sqlx::AnyPool, tenant_id: &str, key: &str) -> Option<Value> {
    let row = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, CAST(scope AS TEXT) AS scope_text, status, audit_ref \
         FROM core_deletionrequest WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1",
    )
    .bind(tenant_id)
    .bind(key)
    .fetch_optional(pool)
    .await
    .ok()??;
    let scope_text: String = row
        .try_get("scope_text")
        .unwrap_or_else(|_| "{}".to_string());
    let scope_value: Value = serde_json::from_str(&scope_text).unwrap_or_else(|_| json!({}));
    Some(json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "scope": scope_value.get("scope").cloned().unwrap_or(Value::Null),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "audit_ref": row.try_get::<String, _>("audit_ref").unwrap_or_default(),
        "executed_at": null,
    }))
}

async fn list_deletion_requests(
    state: &AppState,
    pool: &sqlx::AnyPool,
    tenant_id: &str,
) -> Response {
    let ts = |column: &str| {
        if is_postgres(state) {
            format!("to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS {column}")
        } else {
            format!("strftime('%Y-%m-%d %H:%M:%S', {column}) AS {column}")
        }
    };
    let columns = [ts("executed_at"), ts("created_at")].join(", ");
    let select = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(scope AS TEXT) AS scope_text, status, audit_ref, \
         {columns} FROM core_deletionrequest WHERE CAST(tenant_id AS TEXT) = ? \
         ORDER BY created_at DESC"
    );
    let rows = match sqlx::query(&select).bind(tenant_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let scope_text: String = row
                .try_get("scope_text")
                .unwrap_or_else(|_| "{}".to_string());
            let scope_value: Value =
                serde_json::from_str(&scope_text).unwrap_or_else(|_| json!({}));
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "scope": scope_value.get("scope").cloned().unwrap_or(Value::Null),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "audit_ref": row.try_get::<String, _>("audit_ref").unwrap_or_default(),
                "executed_at": timestamp_value(row, "executed_at"),
                "created_at": timestamp_value(row, "created_at"),
            })
        })
        .collect();
    Json(json!({"results": results})).into_response()
}
