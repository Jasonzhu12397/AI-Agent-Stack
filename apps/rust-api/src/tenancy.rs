#![allow(clippy::result_large_err)]

use std::net::SocketAddr;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{NaiveDateTime, SecondsFormat, Utc};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError, SessionUser};
use crate::proxy::AppState;

const TENANT_ROLES: [&str; 4] = ["owner", "admin", "member", "viewer"];
const PROJECT_ROLES: [&str; 3] = ["admin", "editor", "viewer"];

#[derive(Deserialize, Default)]
pub struct PageQuery {
    page: Option<String>,
    page_size: Option<String>,
}

struct RequestMeta {
    request_id: String,
    remote_addr: Option<String>,
    user_agent: String,
}

struct Pagination {
    page: i64,
    page_size: i64,
}

/// Native replacement for the tenant, project and membership endpoints.
///
/// These handlers read and write the same tables Django owns, so the console
/// can switch to the Rust API without a data migration or a second schema.
pub async fn list_tenants(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<PageQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let postgres = is_postgres(&state);
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(DISTINCT t.id) FROM core_tenant t \
         JOIN core_tenantmembership m ON m.tenant_id = t.id WHERE m.user_id = ?",
    )
    .bind(user.id)
    .fetch_one(pool)
    .await;
    let count = match count {
        Ok(count) => count,
        Err(error) => return database_error(error),
    };
    let page = pagination(&query, 10);
    let sql = format!(
        "SELECT CAST(t.id AS TEXT), t.name, t.slug, {} FROM core_tenant t \
         JOIN core_tenantmembership m ON m.tenant_id = t.id \
         WHERE m.user_id = ? ORDER BY t.name LIMIT ? OFFSET ?",
        timestamp_expr(postgres, "t")
    );
    let rows = sqlx::query_as::<_, (String, String, String, String)>(&sql)
        .bind(user.id)
        .bind(page.page_size)
        .bind((page.page - 1) * page.page_size)
        .fetch_all(pool)
        .await;
    match rows {
        Ok(rows) => {
            let results: Vec<Value> = rows
                .into_iter()
                .map(|(id, name, slug, created_at)| {
                    json!({"id": id, "name": name, "slug": slug, "created_at": normalize_timestamp(&created_at)})
                })
                .collect();
            let (next, previous) = page_links(&headers, "/api/v1/tenants/", &page, count);
            Json(json!({"count": count, "next": next, "previous": previous, "results": results}))
                .into_response()
        }
        Err(error) => database_error(error),
    }
}

pub async fn create_tenant(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let postgres = is_postgres(&state);
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let name = match required_string(&payload, "name", 120) {
        Ok(name) => name,
        Err(response) => return response,
    };
    let slug = match required_slug(&payload, "slug", 80) {
        Ok(slug) => slug,
        Err(response) => return response,
    };
    let tenant_id = uuid::Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?::uuid, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&tenant_id)
        .bind(&name)
        .bind(&slug)
        .bind(&created_at)
        .execute(&mut *transaction)
        .await
    {
        return unique_or_database_error(error, "slug", "Tenant slug already exists.");
    }
    let membership_id = uuid::Uuid::new_v4().to_string();
    let membership_sql = if postgres {
        "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
         VALUES (?::uuid, ?::uuid, ?, 'owner', ?::timestamptz)"
    } else {
        "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
         VALUES (?, ?, ?, 'owner', ?)"
    };
    if let Err(error) = sqlx::query(membership_sql)
        .bind(&membership_id)
        .bind(&tenant_id)
        .bind(user.id)
        .bind(&created_at)
        .execute(&mut *transaction)
        .await
    {
        return database_error(error);
    }
    let meta = request_meta(&headers);
    let event = AuditEvent {
        tenant_id: Some(&tenant_id),
        project_id: None,
        event_type: "tenant.created",
        resource_type: "tenant",
        resource_id: &tenant_id,
        outcome: "success",
        actor_id: Some(user.id),
        request_id: &meta.request_id,
        remote_addr: meta.remote_addr.as_deref(),
        user_agent: &meta.user_agent,
        metadata: json!({"slug": slug, "name": name}),
    };
    if let Err(error) = record_event_in_transaction(&mut transaction, postgres, &event).await {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    (
        StatusCode::CREATED,
        Json(json!({
            "id": tenant_id,
            "name": name,
            "slug": slug,
            "created_at": normalize_timestamp(&created_at),
        })),
    )
        .into_response()
}

pub async fn list_projects(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Query(query): Query<PageQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let admin = role == "owner" || role == "admin";
    let postgres = is_postgres(&state);
    let page = pagination(&query, 10);
    let count_sql = if admin {
        "SELECT COUNT(DISTINCT p.id) FROM core_project p WHERE CAST(p.tenant_id AS TEXT) = ?"
    } else {
        "SELECT COUNT(DISTINCT p.id) FROM core_project p \
         JOIN core_projectmembership pm ON pm.project_id = p.id \
         WHERE CAST(p.tenant_id AS TEXT) = ? AND pm.user_id = ?"
    };
    let mut count_query = sqlx::query_scalar::<_, i64>(count_sql).bind(&tenant_id);
    if !admin {
        count_query = count_query.bind(user.id);
    }
    let count = match count_query.fetch_one(pool).await {
        Ok(count) => count,
        Err(error) => return database_error(error),
    };
    let sql = if admin {
        format!(
            "SELECT CAST(p.id AS TEXT), p.name, p.slug, {} FROM core_project p \
             WHERE CAST(p.tenant_id AS TEXT) = ? ORDER BY p.name LIMIT ? OFFSET ?",
            timestamp_expr(postgres, "p")
        )
    } else {
        format!(
            "SELECT CAST(p.id AS TEXT), p.name, p.slug, {} FROM core_project p \
             JOIN core_projectmembership pm ON pm.project_id = p.id \
             WHERE CAST(p.tenant_id AS TEXT) = ? AND pm.user_id = ? \
             ORDER BY p.name LIMIT ? OFFSET ?",
            timestamp_expr(postgres, "p")
        )
    };
    let mut rows_query =
        sqlx::query_as::<_, (String, String, String, String)>(&sql).bind(&tenant_id);
    if !admin {
        rows_query = rows_query.bind(user.id);
    }
    let rows = match rows_query
        .bind(page.page_size)
        .bind((page.page - 1) * page.page_size)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error),
    };
    let results: Vec<Value> = rows
        .into_iter()
        .map(|(id, name, slug, created_at)| {
            json!({
                "id": id,
                "tenant_id": tenant_id,
                "name": name,
                "slug": slug,
                "created_at": normalize_timestamp(&created_at),
            })
        })
        .collect();
    let (next, previous) = page_links(
        &headers,
        &format!("/api/v1/tenants/{tenant_id}/projects/"),
        &page,
        count,
    );
    Json(json!({"count": count, "next": next, "previous": previous, "results": results}))
        .into_response()
}

pub async fn create_project(
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
        return service_unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return error(
            "PERMISSION_DENIED",
            "Permission denied.",
            StatusCode::FORBIDDEN,
        );
    }
    let postgres = is_postgres(&state);
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let name = match required_string(&payload, "name", 120) {
        Ok(name) => name,
        Err(response) => return response,
    };
    let slug = match required_slug(&payload, "slug", 80) {
        Ok(slug) => slug,
        Err(response) => return response,
    };
    let project_id = uuid::Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_project (id, tenant_id, name, slug, created_at) \
         VALUES (?::uuid, ?::uuid, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&project_id)
        .bind(&tenant_id)
        .bind(&name)
        .bind(&slug)
        .bind(&created_at)
        .execute(&mut *transaction)
        .await
    {
        return unique_or_database_error(
            error,
            "slug",
            "Project slug already exists in this tenant.",
        );
    }
    let membership_id = uuid::Uuid::new_v4().to_string();
    let membership_sql = if postgres {
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?::uuid, ?::uuid, ?, 'admin', ?::timestamptz)"
    } else {
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?, ?, ?, 'admin', ?)"
    };
    if let Err(error) = sqlx::query(membership_sql)
        .bind(&membership_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&created_at)
        .execute(&mut *transaction)
        .await
    {
        return database_error(error);
    }
    let metadata = json!({"slug": slug, "name": name});
    if let Err(error) = audit_write(
        &mut transaction,
        &state,
        &tenant_id,
        Some(&project_id),
        "project.created",
        "project",
        &project_id,
        "success",
        user.id,
        &headers,
        metadata,
    )
    .await
    {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    (
        StatusCode::CREATED,
        Json(json!({
            "id": project_id,
            "tenant_id": tenant_id,
            "name": name,
            "slug": slug,
            "created_at": normalize_timestamp(&created_at),
        })),
    )
        .into_response()
}

pub async fn list_tenant_members(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return error(
            "TENANT_ADMIN_REQUIRED",
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    match tenant_member_rows(pool, &tenant_id).await {
        Ok(rows) => Json(rows).into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn create_tenant_member(
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
        return service_unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return error(
            "TENANT_ADMIN_REQUIRED",
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let username = match payload.get("username").and_then(Value::as_str) {
        Some(value) if !value.is_empty() => value.to_string(),
        _ => {
            return error(
                "MEMBERSHIP_INPUT_INVALID",
                "A valid username and role are required.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let selected_role = payload
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or("member")
        .to_string();
    if !TENANT_ROLES.contains(&selected_role.as_str()) {
        return error(
            "MEMBERSHIP_INPUT_INVALID",
            "A valid username and role are required.",
            StatusCode::BAD_REQUEST,
        );
    }
    if selected_role == "owner" && role != "owner" {
        return error(
            "OWNER_ROLE_REQUIRED",
            "Only an owner can grant the owner role.",
            StatusCode::FORBIDDEN,
        );
    }
    let target = sqlx::query_as::<_, (i64, String, String)>(
        "SELECT id, username, email FROM auth_user WHERE username = ?",
    )
    .bind(&username)
    .fetch_optional(pool)
    .await;
    let target = match target {
        Ok(Some(target)) => target,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if let Ok(Some(_)) = tenant_role(pool, &tenant_id, target.0).await {
        return error(
            "MEMBERSHIP_EXISTS",
            "User is already a tenant member.",
            StatusCode::CONFLICT,
        );
    }
    let postgres = is_postgres(&state);
    let membership_id = uuid::Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
         VALUES (?::uuid, ?::uuid, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
         VALUES (?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&membership_id)
        .bind(&tenant_id)
        .bind(target.0)
        .bind(&selected_role)
        .bind(&created_at)
        .execute(&mut *transaction)
        .await
    {
        return unique_or_database_error(error, "username", "User is already a tenant member.");
    }
    let metadata = json!({"username": target.1, "role": selected_role});
    if let Err(error) = audit_write(
        &mut transaction,
        &state,
        &tenant_id,
        None,
        "tenant.member.added",
        "tenant_membership",
        &membership_id,
        "success",
        user.id,
        &headers,
        metadata,
    )
    .await
    {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    (
        StatusCode::CREATED,
        Json(json!({
            "id": membership_id,
            "user_id": target.0,
            "username": target.1,
            "email": target.2,
            "role": selected_role,
        })),
    )
        .into_response()
}

pub async fn update_tenant_member(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, membership_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let actor_role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if actor_role != "owner" && actor_role != "admin" {
        return error(
            "TENANT_ADMIN_REQUIRED",
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let new_role = match payload.get("role").and_then(Value::as_str) {
        Some(role) if TENANT_ROLES.contains(&role) => role.to_string(),
        _ => {
            return error(
                "MEMBERSHIP_ROLE_INVALID",
                "A valid role is required.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    if postgres {
        if let Err(error) = sqlx::query("SELECT id FROM core_tenant WHERE id = $1::uuid FOR UPDATE")
            .bind(&tenant_id)
            .execute(&mut *transaction)
            .await
        {
            return database_error(error);
        }
    }
    let membership = sqlx::query_as::<_, (String, i64, String, String)>(
        "SELECT role, user_id, username, email FROM core_tenantmembership m \
         JOIN auth_user u ON u.id = m.user_id \
         WHERE CAST(m.id AS TEXT) = ? AND CAST(m.tenant_id AS TEXT) = ?",
    )
    .bind(&membership_id)
    .bind(&tenant_id)
    .fetch_optional(&mut *transaction)
    .await;
    let (old_role, target_id, target_username, target_email) = match membership {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if (old_role == "owner" || new_role == "owner") && actor_role != "owner" {
        let message = if old_role == "owner" {
            "Only an owner can change an owner role."
        } else {
            "Only an owner can grant the owner role."
        };
        return error("OWNER_ROLE_REQUIRED", message, StatusCode::FORBIDDEN);
    }
    if old_role == "owner" && new_role != "owner" {
        let owners = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_tenantmembership \
             WHERE CAST(tenant_id AS TEXT) = ? AND role = 'owner'",
        )
        .bind(&tenant_id)
        .fetch_one(&mut *transaction)
        .await;
        match owners {
            Ok(count) if count <= 1 => {
                return error(
                    "LAST_OWNER_PROTECTED",
                    "A tenant must retain at least one owner.",
                    StatusCode::CONFLICT,
                )
            }
            Ok(_) => {}
            Err(error) => return database_error(error),
        }
    }
    if let Err(error) =
        sqlx::query("UPDATE core_tenantmembership SET role = ? WHERE CAST(id AS TEXT) = ?")
            .bind(&new_role)
            .bind(&membership_id)
            .execute(&mut *transaction)
            .await
    {
        return database_error(error);
    }
    let metadata = json!({
        "username": target_username,
        "old_role": old_role,
        "new_role": new_role,
    });
    if let Err(error) = audit_write(
        &mut transaction,
        &state,
        &tenant_id,
        None,
        "tenant.member.role_changed",
        "tenant_membership",
        &membership_id,
        "success",
        user.id,
        &headers,
        metadata,
    )
    .await
    {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    Json(json!({
        "id": membership_id,
        "user_id": target_id,
        "username": target_username,
        "email": target_email,
        "role": new_role,
    }))
    .into_response()
}

pub async fn delete_tenant_member(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, membership_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    let actor_role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if actor_role != "owner" && actor_role != "admin" {
        return error(
            "TENANT_ADMIN_REQUIRED",
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    if postgres {
        if let Err(error) = sqlx::query("SELECT id FROM core_tenant WHERE id = $1::uuid FOR UPDATE")
            .bind(&tenant_id)
            .execute(&mut *transaction)
            .await
        {
            return database_error(error);
        }
    }
    let membership = sqlx::query_as::<_, (String, i64, String)>(
        "SELECT role, user_id, username FROM core_tenantmembership m \
         JOIN auth_user u ON u.id = m.user_id \
         WHERE CAST(m.id AS TEXT) = ? AND CAST(m.tenant_id AS TEXT) = ?",
    )
    .bind(&membership_id)
    .bind(&tenant_id)
    .fetch_optional(&mut *transaction)
    .await;
    let (role, target_id, target_username) = match membership {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if role == "owner" {
        if actor_role != "owner" {
            return error(
                "OWNER_ROLE_REQUIRED",
                "Only an owner can remove an owner.",
                StatusCode::FORBIDDEN,
            );
        }
        let owners = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_tenantmembership \
             WHERE CAST(tenant_id AS TEXT) = ? AND role = 'owner'",
        )
        .bind(&tenant_id)
        .fetch_one(&mut *transaction)
        .await;
        match owners {
            Ok(count) if count <= 1 => {
                return error(
                    "LAST_OWNER_PROTECTED",
                    "A tenant must retain at least one owner.",
                    StatusCode::CONFLICT,
                )
            }
            Ok(_) => {}
            Err(error) => return database_error(error),
        }
    }
    if let Err(error) = sqlx::query(
        "DELETE FROM core_projectmembership WHERE user_id = ? AND project_id IN \
         (SELECT id FROM core_project WHERE CAST(tenant_id AS TEXT) = ?)",
    )
    .bind(target_id)
    .bind(&tenant_id)
    .execute(&mut *transaction)
    .await
    {
        return database_error(error);
    }
    if let Err(error) = sqlx::query("DELETE FROM core_tenantmembership WHERE CAST(id AS TEXT) = ?")
        .bind(&membership_id)
        .execute(&mut *transaction)
        .await
    {
        return database_error(error);
    }
    let metadata = json!({"username": target_username, "role": role});
    if let Err(error) = audit_write(
        &mut transaction,
        &state,
        &tenant_id,
        None,
        "tenant.member.removed",
        "tenant_membership",
        &membership_id,
        "success",
        user.id,
        &headers,
        metadata,
    )
    .await
    {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    StatusCode::NO_CONTENT.into_response()
}

pub async fn list_project_members(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let tenant_admin = match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    };
    if !tenant_admin
        && !match project_admin(pool, &project_id, user.id).await {
            Ok(value) => value,
            Err(error) => return database_error_message(error),
        }
    {
        return error(
            "PROJECT_ADMIN_REQUIRED",
            "Project administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    match project_member_rows(pool, &project_id).await {
        Ok(rows) => Json(rows).into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn create_project_member(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let tenant_admin = match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    };
    if !tenant_admin
        && !match project_admin(pool, &project_id, user.id).await {
            Ok(value) => value,
            Err(error) => return database_error_message(error),
        }
    {
        return error(
            "PROJECT_ADMIN_REQUIRED",
            "Project administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let username = match payload.get("username").and_then(Value::as_str) {
        Some(value) if !value.is_empty() => value.to_string(),
        _ => {
            return error(
                "MEMBERSHIP_INPUT_INVALID",
                "A valid username and role are required.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let selected_role = payload
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or("viewer")
        .to_string();
    if !PROJECT_ROLES.contains(&selected_role.as_str()) {
        return error(
            "MEMBERSHIP_INPUT_INVALID",
            "A valid username and role are required.",
            StatusCode::BAD_REQUEST,
        );
    }
    let target = sqlx::query_as::<_, (i64, String, String)>(
        "SELECT id, username, email FROM auth_user WHERE username = ?",
    )
    .bind(&username)
    .fetch_optional(pool)
    .await;
    let target = match target {
        Ok(Some(target)) => target,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let tenant_member = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(&tenant_id)
    .bind(target.0)
    .fetch_one(pool)
    .await;
    match tenant_member {
        Ok(0) => {
            return error(
                "PROJECT_MEMBER_NOT_IN_TENANT",
                "User must belong to the tenant before joining a project.",
                StatusCode::CONFLICT,
            )
        }
        Ok(_) => {}
        Err(error) => return database_error(error),
    }
    let exists = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(&project_id)
    .bind(target.0)
    .fetch_one(pool)
    .await;
    match exists {
        Ok(0) => {}
        Ok(_) => {
            return error(
                "MEMBERSHIP_EXISTS",
                "User is already a project member.",
                StatusCode::CONFLICT,
            )
        }
        Err(error) => return database_error(error),
    }
    let membership_id = uuid::Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let insert = if postgres {
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?::uuid, ?::uuid, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&membership_id)
        .bind(&project_id)
        .bind(target.0)
        .bind(&selected_role)
        .bind(&created_at)
        .execute(pool)
        .await
    {
        return unique_or_database_error(error, "username", "User is already a project member.");
    }
    (
        StatusCode::CREATED,
        Json(json!({
            "id": membership_id,
            "user_id": target.0,
            "username": target.1,
            "email": target.2,
            "role": selected_role,
        })),
    )
        .into_response()
}

pub async fn update_project_member(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, membership_id)): Path<(String, String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let tenant_admin = match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    };
    if !tenant_admin
        && !match project_admin(pool, &project_id, user.id).await {
            Ok(value) => value,
            Err(error) => return database_error_message(error),
        }
    {
        return error(
            "PROJECT_ADMIN_REQUIRED",
            "Project administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let new_role = match payload.get("role").and_then(Value::as_str) {
        Some(role) if PROJECT_ROLES.contains(&role) => role.to_string(),
        _ => {
            return error(
                "MEMBERSHIP_ROLE_INVALID",
                "A valid role is required.",
                StatusCode::BAD_REQUEST,
            )
        }
    };
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    if postgres {
        if let Err(error) =
            sqlx::query("SELECT id FROM core_project WHERE id = $1::uuid FOR UPDATE")
                .bind(&project_id)
                .execute(&mut *transaction)
                .await
        {
            return database_error(error);
        }
    }
    let membership = sqlx::query_as::<_, (String, i64, String, String)>(
        "SELECT role, user_id, username, email FROM core_projectmembership m \
         JOIN auth_user u ON u.id = m.user_id \
         WHERE CAST(m.id AS TEXT) = ? AND CAST(m.project_id AS TEXT) = ?",
    )
    .bind(&membership_id)
    .bind(&project_id)
    .fetch_optional(&mut *transaction)
    .await;
    let (old_role, target_id, target_username, target_email) = match membership {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if old_role == "admin" && new_role != "admin" {
        let admins = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_projectmembership \
             WHERE CAST(project_id AS TEXT) = ? AND role = 'admin'",
        )
        .bind(&project_id)
        .fetch_one(&mut *transaction)
        .await;
        match admins {
            Ok(count) if count <= 1 => {
                return error(
                    "LAST_PROJECT_ADMIN_PROTECTED",
                    "A project must retain at least one administrator.",
                    StatusCode::CONFLICT,
                )
            }
            Ok(_) => {}
            Err(error) => return database_error(error),
        }
    }
    if let Err(error) =
        sqlx::query("UPDATE core_projectmembership SET role = ? WHERE CAST(id AS TEXT) = ?")
            .bind(&new_role)
            .bind(&membership_id)
            .execute(&mut *transaction)
            .await
    {
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    Json(json!({
        "id": membership_id,
        "user_id": target_id,
        "username": target_username,
        "email": target_email,
        "role": new_role,
    }))
    .into_response()
}

pub async fn delete_project_member(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, membership_id)): Path<(String, String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let tenant_admin = match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    };
    if !tenant_admin
        && !match project_admin(pool, &project_id, user.id).await {
            Ok(value) => value,
            Err(error) => return database_error_message(error),
        }
    {
        return error(
            "PROJECT_ADMIN_REQUIRED",
            "Project administrator permission required.",
            StatusCode::FORBIDDEN,
        );
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    if postgres {
        if let Err(error) =
            sqlx::query("SELECT id FROM core_project WHERE id = $1::uuid FOR UPDATE")
                .bind(&project_id)
                .execute(&mut *transaction)
                .await
        {
            return database_error(error);
        }
    }
    let membership = sqlx::query_as::<_, (String,)>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(id AS TEXT) = ? AND CAST(project_id AS TEXT) = ?",
    )
    .bind(&membership_id)
    .bind(&project_id)
    .fetch_optional(&mut *transaction)
    .await;
    let role = match membership {
        Ok(Some((role,))) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if role == "admin" {
        let admins = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_projectmembership \
             WHERE CAST(project_id AS TEXT) = ? AND role = 'admin'",
        )
        .bind(&project_id)
        .fetch_one(&mut *transaction)
        .await;
        match admins {
            Ok(count) if count <= 1 => {
                return error(
                    "LAST_PROJECT_ADMIN_PROTECTED",
                    "A project must retain at least one administrator.",
                    StatusCode::CONFLICT,
                )
            }
            Ok(_) => {}
            Err(error) => return database_error(error),
        }
    }
    if let Err(error) = sqlx::query("DELETE FROM core_projectmembership WHERE CAST(id AS TEXT) = ?")
        .bind(&membership_id)
        .execute(&mut *transaction)
        .await
    {
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    StatusCode::NO_CONTENT.into_response()
}

async fn require_user(state: &AppState, headers: &HeaderMap) -> Result<SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(service_unavailable());
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return Err(service_unavailable());
    };
    match authenticate(pool, secret_key, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(_)) => Err(error(
            "AUTHENTICATION_REQUIRED",
            "Authentication required.",
            StatusCode::UNAUTHORIZED,
        )),
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust api could not resolve the session");
            Err(service_unavailable())
        }
    }
}

fn error(code: &str, message: &str, status: StatusCode) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn not_found() -> Response {
    error("NOT_FOUND", "Resource not found.", StatusCode::NOT_FOUND)
}

fn service_unavailable() -> Response {
    error(
        "SERVICE_UNAVAILABLE",
        "The service is temporarily unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
    )
}

fn database_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust api database operation failed");
    service_unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust api database operation failed");
    service_unavailable()
}

fn is_postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .map(|url| url.starts_with("postgres://") || url.starts_with("postgresql://"))
        .unwrap_or(false)
}

fn timestamp_expr(postgres: bool, alias: &str) -> String {
    if postgres {
        format!(
            "to_char({alias}.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
        )
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.created_at)")
    }
}

fn normalize_timestamp(value: &str) -> String {
    if let Ok(timestamp) = chrono::DateTime::parse_from_rfc3339(value) {
        return format_timestamp(timestamp.with_timezone(&Utc));
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.fZ", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(timestamp) = NaiveDateTime::parse_from_str(value, format) {
            return format_timestamp(timestamp.and_utc());
        }
    }
    value.to_string()
}

fn format_timestamp(timestamp: chrono::DateTime<Utc>) -> String {
    if timestamp.timestamp_subsec_micros() == 0 {
        timestamp.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        timestamp.format("%Y-%m-%dT%H:%M:%S.%6fZ").to_string()
    }
}

fn parse_body(body: &Bytes) -> Result<Value, Response> {
    let value: Value = serde_json::from_slice(body).map_err(|_| {
        error(
            "INVALID_REQUEST",
            "Request could not be processed.",
            StatusCode::BAD_REQUEST,
        )
    })?;
    if !value.is_object() {
        return Err(error(
            "INVALID_REQUEST",
            "Request could not be processed.",
            StatusCode::BAD_REQUEST,
        ));
    }
    Ok(value)
}

fn validation_error(field: &str, message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Request could not be processed.",
                "details": { field: [message] },
            }
        })),
    )
        .into_response()
}

fn required_string(payload: &Value, field: &str, max: usize) -> Result<String, Response> {
    match payload.get(field).and_then(Value::as_str) {
        Some(value) if !value.trim().is_empty() && value.chars().count() <= max => {
            Ok(value.to_string())
        }
        Some(value) if value.trim().is_empty() => {
            Err(validation_error(field, "This field may not be blank."))
        }
        Some(_) => Err(validation_error(
            field,
            &format!("Ensure this field has no more than {max} characters."),
        )),
        None => Err(validation_error(field, "This field is required.")),
    }
}

fn required_slug(payload: &Value, field: &str, max: usize) -> Result<String, Response> {
    let value = required_string(payload, field, max)?;
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '-' || character == '_')
    {
        Ok(value)
    } else {
        Err(validation_error(
            field,
            "Enter a valid “slug” consisting of letters, numbers, underscores or hyphens.",
        ))
    }
}

fn unique_or_database_error(failure: sqlx::Error, field: &str, message: &str) -> Response {
    let text = failure.to_string().to_lowercase();
    if text.contains("unique") || text.contains("duplicate") {
        if field == "username" {
            return error("MEMBERSHIP_EXISTS", message, StatusCode::CONFLICT);
        }
        return validation_error(field, message);
    }
    database_error(failure)
}

fn pagination(query: &PageQuery, default_size: i64) -> Pagination {
    let page = query
        .page
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(1);
    let page_size = query
        .page_size
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default_size)
        .min(100);
    Pagination { page, page_size }
}

fn page_links(
    headers: &HeaderMap,
    path: &str,
    page: &Pagination,
    count: i64,
) -> (Option<String>, Option<String>) {
    let base = absolute_base(headers);
    let pages = if count == 0 {
        0
    } else {
        (count + page.page_size - 1) / page.page_size
    };
    let next = if page.page < pages {
        Some(format!(
            "{base}{path}?page={}&page_size={}",
            page.page + 1,
            page.page_size
        ))
    } else {
        None
    };
    let previous = if page.page > 1 {
        Some(format!(
            "{base}{path}?page={}&page_size={}",
            page.page - 1,
            page.page_size
        ))
    } else {
        None
    };
    (next, previous)
}

fn absolute_base(headers: &HeaderMap) -> String {
    let proto = headers
        .get("x-forwarded-proto")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .unwrap_or("http");
    let host = headers
        .get("x-forwarded-host")
        .and_then(|value| value.to_str().ok())
        .or_else(|| headers.get("host").and_then(|value| value.to_str().ok()))
        .unwrap_or("localhost");
    format!("{proto}://{host}")
}

fn request_meta(headers: &HeaderMap) -> RequestMeta {
    let request_id = headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .chars()
        .take(64)
        .collect();
    let user_agent = headers
        .get("user-agent")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .chars()
        .take(256)
        .collect();
    let remote_addr = headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .map(str::trim)
        .and_then(|value| value.parse::<SocketAddr>().ok())
        .map(|address| address.ip().to_string())
        .or_else(|| {
            headers
                .get("x-real-ip")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<std::net::IpAddr>().ok())
                .map(|address| address.to_string())
        });
    RequestMeta {
        request_id,
        remote_addr,
        user_agent,
    }
}

async fn tenant_role(
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    user_id: i64,
) -> Result<Option<String>, String> {
    sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())
}

async fn tenant_admin(pool: &sqlx::AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, String> {
    Ok(matches!(
        tenant_role(pool, tenant_id, user_id).await?,
        Some(role) if role == "owner" || role == "admin"
    ))
}

async fn project_access(
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    let project = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_project \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
    )
    .bind(project_id)
    .bind(tenant_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    if project == 0 {
        return Ok(false);
    }
    if tenant_admin(pool, tenant_id, user_id).await? {
        return Ok(true);
    }
    let member = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(member > 0)
}

async fn project_admin(
    pool: &sqlx::AnyPool,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(role.as_deref() == Some("admin"))
}

async fn tenant_member_rows(pool: &sqlx::AnyPool, tenant_id: &str) -> Result<Vec<Value>, String> {
    let rows = sqlx::query_as::<_, (String, i64, String, String, String)>(
        "SELECT CAST(m.id AS TEXT), m.user_id, u.username, u.email, m.role \
         FROM core_tenantmembership m JOIN auth_user u ON u.id = m.user_id \
         WHERE CAST(m.tenant_id AS TEXT) = ? ORDER BY u.username",
    )
    .bind(tenant_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|(id, user_id, username, email, role)| {
            json!({"id": id, "user_id": user_id, "username": username, "email": email, "role": role})
        })
        .collect())
}

async fn project_member_rows(pool: &sqlx::AnyPool, project_id: &str) -> Result<Vec<Value>, String> {
    let rows = sqlx::query_as::<_, (String, i64, String, String, String)>(
        "SELECT CAST(m.id AS TEXT), m.user_id, u.username, u.email, m.role \
         FROM core_projectmembership m JOIN auth_user u ON u.id = m.user_id \
         WHERE CAST(m.project_id AS TEXT) = ? ORDER BY u.username",
    )
    .bind(project_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|(id, user_id, username, email, role)| {
            json!({"id": id, "user_id": user_id, "username": username, "email": email, "role": role})
        })
        .collect())
}

#[allow(clippy::too_many_arguments)]
async fn audit_write(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    state: &AppState,
    tenant_id: &str,
    project_id: Option<&str>,
    event_type: &str,
    resource_type: &str,
    resource_id: &str,
    outcome: &str,
    actor_id: i64,
    headers: &HeaderMap,
    metadata: Value,
) -> Result<(), String> {
    let meta = request_meta(headers);
    let event = AuditEvent {
        tenant_id: Some(tenant_id),
        project_id,
        event_type,
        resource_type,
        resource_id,
        outcome,
        actor_id: Some(actor_id),
        request_id: &meta.request_id,
        remote_addr: meta.remote_addr.as_deref(),
        user_agent: &meta.user_agent,
        metadata,
    };
    record_event_in_transaction(transaction, is_postgres(state), &event).await
}
