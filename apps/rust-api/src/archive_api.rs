#![allow(clippy::result_large_err)]

//! Native archive listing, restore and purge for project-owned objects.

use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use sqlx::Row;

use crate::proxy::AppState;

const ITEM_TYPES: [&str; 3] = ["agent", "agent_team", "knowledge_base"];

fn fail(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message}})),
    )
        .into_response()
}

fn now_column(state: &AppState, alias: &str, column: &str) -> String {
    let postgres = state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"));
    if postgres {
        format!("to_char({alias}.{column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%d %H:%M:%S', {alias}.{column})")
    }
}

fn table_for(item_type: &str) -> Option<&'static str> {
    match item_type {
        "agent" => Some("core_agentdefinition"),
        "agent_team" => Some("core_agentteam"),
        "knowledge_base" => Some("core_knowledgebase"),
        _ => None,
    }
}

async fn project_admin(
    pool: &sqlx::AnyPool,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    let row = sqlx::query(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(row
        .and_then(|row| row.try_get::<String, _>("role").ok())
        .is_some_and(|role| role == "admin" || role == "owner"))
}

/// `GET /api/v1/tenants/{tenant}/projects/{project}/archive-items/`
pub async fn list_archive_items(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Query(params): Query<std::collections::HashMap<String, String>>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    if !matches!(
        crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await,
        Ok(true)
    ) {
        return fail(
            StatusCode::NOT_FOUND,
            "PROJECT_NOT_FOUND",
            "Project not found.",
        );
    }
    let requested = params.get("type").cloned().unwrap_or_default();
    if !requested.is_empty() && table_for(&requested).is_none() {
        return fail(
            StatusCode::BAD_REQUEST,
            "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
            &format!("Unknown archive item type: {requested}"),
        );
    }
    let q = params
        .get("q")
        .cloned()
        .unwrap_or_default()
        .trim()
        .to_string();
    let page: i64 = params
        .get("page")
        .and_then(|value| value.parse().ok())
        .unwrap_or(1)
        .max(1);
    let types: Vec<&str> = if requested.is_empty() {
        ITEM_TYPES.to_vec()
    } else {
        vec![requested.as_str()]
    };
    let mut results = Vec::<Value>::new();
    for item_type in types {
        let table = table_for(item_type).unwrap();
        let updated = now_column(&state, "item", "updated_at");
        let description = "item.description";
        let sql = format!(
            "SELECT CAST(item.id AS TEXT) AS id, CAST(item.project_id AS TEXT) AS project_id, \
             item.name, item.slug, COALESCE({description}, '') AS description, {updated} AS updated \
             FROM {table} item WHERE CAST(item.tenant_id AS TEXT) = ? \
             AND CAST(item.project_id AS TEXT) = ? AND item.is_archived = 1 \
             AND (? = '' OR LOWER(item.name) LIKE '%' || LOWER(?) || '%')"
        );
        let rows = match sqlx::query(&sql)
            .bind(&tenant_id)
            .bind(&project_id)
            .bind(&q)
            .bind(&q)
            .fetch_all(pool)
            .await
        {
            Ok(rows) => rows,
            Err(error) => {
                return fail(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "DATABASE_ERROR",
                    &error.to_string(),
                )
            }
        };
        for row in rows {
            results.push(json!({
                "item_type": item_type,
                "item_id": row.try_get::<String, _>("id").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "slug": row.try_get::<String, _>("slug").unwrap_or_default(),
                "description": row.try_get::<String, _>("description").unwrap_or_default(),
                "archived_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
            }));
        }
    }
    results.sort_by(|left, right| {
        left.get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .cmp(right.get("name").and_then(Value::as_str).unwrap_or(""))
    });
    let count = results.len();
    let start = ((page - 1) * 50) as usize;
    let page_items: Vec<Value> = results.into_iter().skip(start).take(50).collect();
    Json(json!({"count": count, "page": page, "results": page_items})).into_response()
}

/// `POST /api/v1/tenants/{tenant}/projects/{project}/archive-items/{item_type}/{item_id}/restore/`
pub async fn restore_archive_item(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, item_type, item_id)): Path<(String, String, String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    if !matches!(
        crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await,
        Ok(true)
    ) {
        return fail(
            StatusCode::NOT_FOUND,
            "PROJECT_NOT_FOUND",
            "Project not found.",
        );
    }
    match project_admin(pool, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => {
            return fail(
                StatusCode::FORBIDDEN,
                "FORBIDDEN",
                "Project admin permission is required.",
            )
        }
        Err(message) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &message,
            )
        }
    }
    let Some(table) = table_for(&item_type) else {
        return fail(
            StatusCode::BAD_REQUEST,
            "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
            &format!("Unknown archive item type: {item_type}"),
        );
    };
    let updated = now_column(&state, "item", "updated_at");
    let sql = format!(
        "SELECT CAST(item.id AS TEXT) AS id, CAST(item.project_id AS TEXT) AS project_id, \
         item.name, item.slug, COALESCE(item.description, '') AS description, \
         CAST(item.is_archived AS INTEGER) AS archived_int, {updated} AS updated \
         FROM {table} item WHERE CAST(item.tenant_id AS TEXT) = ? \
         AND CAST(item.project_id AS TEXT) = ? AND CAST(item.id AS TEXT) = ? LIMIT 1"
    );
    let row = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(&item_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            return fail(
                StatusCode::NOT_FOUND,
                "ARCHIVE_ITEM_NOT_FOUND",
                "Archived item not found in this project.",
            )
        }
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let archived = row.try_get::<i64, _>("archived_int").unwrap_or(0) != 0;
    let payload = json!({
        "item_type": item_type,
        "item_id": row.try_get::<String, _>("id").unwrap_or_default(),
        "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
        "name": row.try_get::<String, _>("name").unwrap_or_default(),
        "slug": row.try_get::<String, _>("slug").unwrap_or_default(),
        "description": row.try_get::<String, _>("description").unwrap_or_default(),
        "archived_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
    });
    if !archived {
        return Json(json!({"already_restored": true, "item": payload})).into_response();
    }
    if let Err(error) = sqlx::query(&format!(
        "UPDATE {table} SET is_archived = 0, updated_at = datetime('now') WHERE CAST(id AS TEXT) = ?"
    ))
    .bind(&item_id)
    .execute(pool)
    .await
    {
        return fail(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error.to_string());
    }
    let _ = sqlx::query(
        "INSERT INTO core_archiveaction (id, tenant_id, project_id, item_type, item_id, action, actor_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, 'restore', ?, ?, datetime('now'))",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(&item_type)
    .bind(&item_id)
    .bind(user.id)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(pool)
    .await;
    Json(json!({"already_restored": false, "item": payload})).into_response()
}

/// `DELETE /api/v1/tenants/{tenant}/projects/{project}/archive-items/{item_type}/{item_id}/`
pub async fn purge_archive_item(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, item_type, item_id)): Path<(String, String, String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    if !matches!(
        crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await,
        Ok(true)
    ) {
        return fail(
            StatusCode::NOT_FOUND,
            "PROJECT_NOT_FOUND",
            "Project not found.",
        );
    }
    match project_admin(pool, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => {
            return fail(
                StatusCode::FORBIDDEN,
                "FORBIDDEN",
                "Project admin permission is required.",
            )
        }
        Err(message) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &message,
            )
        }
    }
    let Some(table) = table_for(&item_type) else {
        return fail(
            StatusCode::BAD_REQUEST,
            "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
            &format!("Unknown archive item type: {item_type}"),
        );
    };
    let row = sqlx::query(&format!(
        "SELECT CAST(item.id AS TEXT) AS id, CAST(item.is_archived AS INTEGER) AS archived_int \
         FROM {table} item WHERE CAST(item.tenant_id AS TEXT) = ? \
         AND CAST(item.project_id AS TEXT) = ? AND CAST(item.id AS TEXT) = ? LIMIT 1"
    ))
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(&item_id)
    .fetch_optional(pool)
    .await;
    let archived = match row {
        Ok(Some(row)) => row.try_get::<i64, _>("archived_int").unwrap_or(0) != 0,
        Ok(None) => false,
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    if !archived {
        return fail(
            StatusCode::NOT_FOUND,
            "ARCHIVE_ITEM_NOT_FOUND",
            "Archived item not found in this project.",
        );
    }
    let _ = sqlx::query(
        "INSERT INTO core_archiveaction (id, tenant_id, project_id, item_type, item_id, action, actor_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, 'purge', ?, ?, datetime('now'))",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(&item_type)
    .bind(&item_id)
    .bind(user.id)
    .bind(uuid::Uuid::new_v4().to_string())
    .execute(pool)
    .await;
    match sqlx::query(&format!("DELETE FROM {table} WHERE CAST(id AS TEXT) = ?"))
        .bind(&item_id)
        .execute(pool)
        .await
    {
        Ok(_) => StatusCode::NO_CONTENT.into_response(),
        Err(error) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        ),
    }
}
