#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access, tenant_admin};

pub async fn list_knowledge_bases(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    match knowledge_base_rows(pool, &state, &tenant_id, &project_id, None).await {
        Ok(rows) => Json(json!({
            "count": rows.len(),
            "results": rows.iter().map(knowledge_base_json).collect::<Vec<_>>(),
        }))
        .into_response(),
        Err(error) => database_error(error),
    }
}

pub async fn create_knowledge_base(
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
        return unavailable();
    };
    if !can_edit(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return legacy_error(
            "Project editor permission required.",
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let name = payload.get("name").and_then(Value::as_str).unwrap_or("");
    if name.trim().is_empty() || name.chars().count() > 120 {
        return legacy_error(
            "name is required and must be at most 120 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let slug = payload.get("slug").and_then(Value::as_str).unwrap_or("");
    if !valid_slug(slug) {
        return legacy_error(
            "slug must be a lowercase URL-safe identifier.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let description = payload
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    if description.chars().count() > 4000 {
        return legacy_error(
            "description must be a string up to 4,000 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let Some(model_id) = payload.get("embedding_model_id").and_then(Value::as_str) else {
        return legacy_error(
            "A valid embedding_model_id is required.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    };
    if uuid::Uuid::parse_str(model_id).is_err() {
        return legacy_error(
            "A valid embedding_model_id is required.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let model = match embedding_model(pool, &state, &tenant_id, user.id, model_id).await {
        Ok(Some(model)) => model,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if !model
        .capabilities
        .iter()
        .any(|capability| capability == "embeddings")
    {
        return legacy_error(
            "Selected model does not declare embeddings capability.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let chunk_size = payload
        .get("chunk_size")
        .and_then(Value::as_i64)
        .unwrap_or(1200);
    let chunk_overlap = payload
        .get("chunk_overlap")
        .and_then(Value::as_i64)
        .unwrap_or(160);
    if !(200..=8000).contains(&chunk_size) || chunk_overlap < 0 || chunk_overlap >= chunk_size {
        return legacy_error(
            "Invalid chunk_size or chunk_overlap.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let duplicate = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_knowledgebase \
         WHERE CAST(project_id AS TEXT) = ? AND slug = ?",
    )
    .bind(&project_id)
    .bind(slug)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if duplicate > 0 {
        return legacy_error(
            "Knowledge base slug already exists in this project.",
            StatusCode::CONFLICT,
            "KNOWLEDGE_BASE_SLUG_TAKEN",
        );
    }
    let knowledge_base_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_knowledgebase (id, tenant_id, project_id, created_by_id, embedding_model_id, \
         name, slug, description, chunk_size, chunk_overlap, is_archived, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?::uuid, ?, ?, ?, ?, ?, FALSE, ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_knowledgebase (id, tenant_id, project_id, created_by_id, embedding_model_id, \
         name, slug, description, chunk_size, chunk_overlap, is_archived, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&knowledge_base_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&model.id)
        .bind(name.trim())
        .bind(slug)
        .bind(description)
        .bind(chunk_size)
        .bind(chunk_overlap)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "knowledge.base.created",
            resource_type: "knowledge_base",
            resource_id: &knowledge_base_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({"slug": slug, "name": name.trim(), "embedding_model_id": model.id}),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match knowledge_base_rows(
        pool,
        &state,
        &tenant_id,
        &project_id,
        Some(&knowledge_base_id),
    )
    .await
    {
        Ok(rows) => match rows.first() {
            Some(record) => {
                (StatusCode::CREATED, Json(knowledge_base_json(record))).into_response()
            }
            None => not_found(),
        },
        Err(error) => database_error(error),
    }
}

pub async fn list_documents(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(knowledge_base_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let Some(_scope) = knowledge_base_scope(pool, &knowledge_base_id, user.id)
        .await
        .ok()
        .flatten()
    else {
        return not_found();
    };
    match document_rows(pool, &state, &knowledge_base_id, None).await {
        Ok(rows) => Json(json!({
            "count": rows.len(),
            "results": rows.iter().map(document_json).collect::<Vec<_>>(),
        }))
        .into_response(),
        Err(error) => database_error(error),
    }
}

pub async fn create_document(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(knowledge_base_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let Some(scope) = knowledge_base_scope(pool, &knowledge_base_id, user.id)
        .await
        .ok()
        .flatten()
    else {
        return not_found();
    };
    if !can_edit(pool, &scope.tenant_id, &scope.project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return legacy_error(
            "Project editor permission required.",
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let title = payload.get("title").and_then(Value::as_str).unwrap_or("");
    if title.trim().is_empty() || title.chars().count() > 240 {
        return legacy_error(
            "title is required and must be at most 240 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let content = payload.get("content").and_then(Value::as_str).unwrap_or("");
    if content.trim().is_empty() || content.chars().count() > 1_000_000 {
        return legacy_error(
            "content must be non-empty and no larger than 1 MB.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let metadata = match payload.get("metadata") {
        Some(Value::Object(map)) => map.clone(),
        Some(_) => {
            return legacy_error(
                "metadata must be a JSON object up to 16 KiB.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            )
        }
        None => Map::new(),
    };
    let metadata_value = Value::Object(metadata.clone());
    if serde_json::to_string(&metadata_value)
        .map(|text| text.len() > 16384)
        .unwrap_or(true)
    {
        return legacy_error(
            "metadata must be a JSON object up to 16 KiB.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    if contains_secret(&metadata_value) {
        return legacy_error(
            "Document metadata cannot contain credential fields.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let checksum = hex::encode(Sha256::digest(content.as_bytes()));
    let document_id = uuid::Uuid::new_v4().to_string();
    let task_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert_document = if postgres {
        "INSERT INTO core_knowledgedocument (id, knowledge_base_id, uploaded_by_id, title, content, \
         metadata, checksum, status, chunk_count, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?, ?, ?, ?::jsonb, ?, 'pending', 0, ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_knowledgedocument (id, knowledge_base_id, uploaded_by_id, title, content, \
         metadata, checksum, status, chunk_count, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert_document)
        .bind(&document_id)
        .bind(&knowledge_base_id)
        .bind(user.id)
        .bind(title.trim())
        .bind(content)
        .bind(Value::Object(metadata).to_string())
        .bind(&checksum)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    let insert_task = if postgres {
        "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, status, \
         idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, 'knowledge.ingest', ?::jsonb, 'queued', ?, 0, 3, \
         ?::timestamptz, ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, status, \
         idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) \
         VALUES (?, ?, ?, ?, 'knowledge.ingest', ?, 'queued', ?, 0, 3, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert_task)
        .bind(&task_id)
        .bind(&scope.tenant_id)
        .bind(&scope.project_id)
        .bind(user.id)
        .bind(json!({"knowledge_document_id": document_id}).to_string())
        .bind(format!("knowledge-ingest:{document_id}"))
        .bind(&now)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&scope.tenant_id),
            project_id: Some(&scope.project_id),
            event_type: "knowledge.document.created",
            resource_type: "knowledge_document",
            resource_id: &document_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "knowledge_base_id": knowledge_base_id,
                "title": title.trim(),
                "checksum": checksum,
            }),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match document_rows(pool, &state, &knowledge_base_id, Some(&document_id)).await {
        Ok(rows) => match rows.first() {
            Some(record) => (StatusCode::ACCEPTED, Json(document_json(record))).into_response(),
            None => not_found(),
        },
        Err(error) => database_error(error),
    }
}

struct KnowledgeBaseRecord {
    id: String,
    tenant_id: String,
    project_id: String,
    name: String,
    slug: String,
    description: String,
    embedding_model_id: String,
    chunk_size: i64,
    chunk_overlap: i64,
    document_count: i64,
    created_at: Option<String>,
    updated_at: Option<String>,
}

fn knowledge_base_json(record: &KnowledgeBaseRecord) -> Value {
    json!({
        "id": record.id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "name": record.name,
        "slug": record.slug,
        "description": record.description,
        "embedding_model_id": record.embedding_model_id,
        "chunk_size": record.chunk_size,
        "chunk_overlap": record.chunk_overlap,
        "is_archived": false,
        "document_count": record.document_count,
        "created_at": record.created_at.as_deref().map(normalize_timestamp),
        "updated_at": record.updated_at.as_deref().map(normalize_timestamp),
    })
}

async fn knowledge_base_rows(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    knowledge_base_id: Option<&str>,
) -> Result<Vec<KnowledgeBaseRecord>, String> {
    let postgres = is_postgres(state);
    let false_literal = if postgres { "FALSE" } else { "0" };
    let created = timestamp(state, "kb", "created_at");
    let updated = timestamp(state, "kb", "updated_at");
    let base = format!(
        "SELECT CAST(kb.id AS TEXT) AS id, CAST(kb.tenant_id AS TEXT) AS tenant_id, \
         CAST(kb.project_id AS TEXT) AS project_id, kb.name, kb.slug, kb.description, \
         CAST(kb.embedding_model_id AS TEXT) AS embedding_model_id, kb.chunk_size, kb.chunk_overlap, \
         (SELECT COUNT(*) FROM core_knowledgedocument doc WHERE CAST(doc.knowledge_base_id AS TEXT) = CAST(kb.id AS TEXT)) AS document_count, \
         {created} AS created, {updated} AS updated FROM core_knowledgebase kb \
         WHERE CAST(kb.tenant_id AS TEXT) = ? AND CAST(kb.project_id AS TEXT) = ? \
           AND kb.is_archived = {false_literal}"
    );
    let rows = if let Some(id) = knowledge_base_id {
        let sql = format!("{base} AND CAST(kb.id AS TEXT) = ?");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .bind(id)
            .fetch_all(pool)
            .await
    } else {
        let sql = format!("{base} ORDER BY kb.name LIMIT 100");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .fetch_all(pool)
            .await
    }
    .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| KnowledgeBaseRecord {
            id: row.try_get("id").unwrap_or_default(),
            tenant_id: row.try_get("tenant_id").unwrap_or_default(),
            project_id: row.try_get("project_id").unwrap_or_default(),
            name: row.try_get("name").unwrap_or_default(),
            slug: row.try_get("slug").unwrap_or_default(),
            description: row.try_get("description").unwrap_or_default(),
            embedding_model_id: row.try_get("embedding_model_id").unwrap_or_default(),
            chunk_size: row.try_get("chunk_size").unwrap_or_default(),
            chunk_overlap: row.try_get("chunk_overlap").unwrap_or_default(),
            document_count: row.try_get("document_count").unwrap_or_default(),
            created_at: row.try_get("created").ok(),
            updated_at: row.try_get("updated").ok(),
        })
        .collect())
}

struct DocumentRecord {
    id: String,
    knowledge_base_id: String,
    title: String,
    metadata: Value,
    checksum: String,
    status: String,
    chunk_count: i64,
    error: Value,
    ingested_at: Option<String>,
    created_at: Option<String>,
    updated_at: Option<String>,
}

fn document_json(record: &DocumentRecord) -> Value {
    json!({
        "id": record.id,
        "knowledge_base_id": record.knowledge_base_id,
        "title": record.title,
        "metadata": record.metadata,
        "checksum": record.checksum,
        "status": record.status,
        "chunk_count": record.chunk_count,
        "error": record.error,
        "ingested_at": record.ingested_at.as_deref().map(normalize_timestamp),
        "created_at": record.created_at.as_deref().map(normalize_timestamp),
        "updated_at": record.updated_at.as_deref().map(normalize_timestamp),
    })
}

async fn document_rows(
    pool: &AnyPool,
    state: &AppState,
    knowledge_base_id: &str,
    document_id: Option<&str>,
) -> Result<Vec<DocumentRecord>, String> {
    let postgres = is_postgres(state);
    let metadata = json_text(postgres, "doc.metadata");
    let error = json_text(postgres, "doc.error");
    let ingested = timestamp(state, "doc", "ingested_at");
    let created = timestamp(state, "doc", "created_at");
    let updated = timestamp(state, "doc", "updated_at");
    let base = format!(
        "SELECT CAST(doc.id AS TEXT) AS id, CAST(doc.knowledge_base_id AS TEXT) AS knowledge_base_id, \
         doc.title, {metadata} AS metadata_text, doc.checksum, doc.status, doc.chunk_count, \
         {error} AS error_text, {ingested} AS ingested, {created} AS created, {updated} AS updated \
         FROM core_knowledgedocument doc WHERE CAST(doc.knowledge_base_id AS TEXT) = ?"
    );
    let rows = if let Some(id) = document_id {
        let sql = format!("{base} AND CAST(doc.id AS TEXT) = ?");
        sqlx::query(&sql)
            .bind(knowledge_base_id)
            .bind(id)
            .fetch_all(pool)
            .await
    } else {
        let sql = format!("{base} ORDER BY doc.created_at DESC LIMIT 100");
        sqlx::query(&sql)
            .bind(knowledge_base_id)
            .fetch_all(pool)
            .await
    }
    .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| DocumentRecord {
            id: row.try_get("id").unwrap_or_default(),
            knowledge_base_id: row.try_get("knowledge_base_id").unwrap_or_default(),
            title: row.try_get("title").unwrap_or_default(),
            metadata: parse_json(
                row.try_get::<Option<String>, _>("metadata_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or_else(|| json!({})),
            checksum: row.try_get("checksum").unwrap_or_default(),
            status: row.try_get("status").unwrap_or_default(),
            chunk_count: row.try_get("chunk_count").unwrap_or_default(),
            error: parse_json(
                row.try_get::<Option<String>, _>("error_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or(Value::Null),
            ingested_at: row.try_get("ingested").ok(),
            created_at: row.try_get("created").ok(),
            updated_at: row.try_get("updated").ok(),
        })
        .collect())
}

struct EmbeddingModel {
    id: String,
    capabilities: Vec<String>,
}

async fn embedding_model(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
    model_id: &str,
) -> Result<Option<EmbeddingModel>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let capabilities = if postgres {
        "config.capabilities::text"
    } else {
        "config.capabilities"
    };
    let sql = format!(
        "SELECT CAST(config.id AS TEXT) AS id, {capabilities} AS capabilities \
         FROM core_modelconfig config JOIN core_modelprovider provider ON provider.id = config.provider_id \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {true_literal} \
           AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)"
    );
    let row = sqlx::query(&sql)
        .bind(model_id)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.map(|row| EmbeddingModel {
        id: row.try_get("id").unwrap_or_default(),
        capabilities: parse_string_array(
            row.try_get::<Option<String>, _>("capabilities")
                .ok()
                .flatten(),
        ),
    }))
}

struct KnowledgeBaseScope {
    tenant_id: String,
    project_id: String,
}

async fn knowledge_base_scope(
    pool: &AnyPool,
    knowledge_base_id: &str,
    user_id: i64,
) -> Result<Option<KnowledgeBaseScope>, String> {
    let row = sqlx::query(
        "SELECT CAST(kb.tenant_id AS TEXT) AS tenant_id, CAST(kb.project_id AS TEXT) AS project_id \
         FROM core_knowledgebase kb \
         JOIN core_tenantmembership membership ON membership.tenant_id = kb.tenant_id \
         WHERE CAST(kb.id AS TEXT) = ? AND kb.is_archived = FALSE AND membership.user_id = ? \
         LIMIT 1",
    )
    .bind(knowledge_base_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    let scope = KnowledgeBaseScope {
        tenant_id: row.try_get("tenant_id").unwrap_or_default(),
        project_id: row.try_get("project_id").unwrap_or_default(),
    };
    let member = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(&scope.project_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if member > 0
        || tenant_admin(pool, &scope.tenant_id, user_id)
            .await
            .unwrap_or(false)
    {
        Ok(Some(scope))
    } else {
        Ok(None)
    }
}

async fn can_edit(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    if tenant_admin(pool, tenant_id, user_id).await? {
        return Ok(true);
    }
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(matches!(role.as_deref(), Some("admin") | Some("editor")))
}

fn timestamp(state: &AppState, alias: &str, field: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{field})")
    }
}

fn json_text(postgres: bool, expression: &str) -> String {
    if postgres {
        format!("{expression}::text")
    } else {
        expression.to_string()
    }
}

fn parse_json(value: Option<String>) -> Option<Value> {
    value.and_then(|text| serde_json::from_str::<Value>(&text).ok())
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

fn contains_secret(value: &Value) -> bool {
    const SECRET_NAMES: [&str; 10] = [
        "api_key",
        "apikey",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "authorization",
        "private_key",
        "client_secret",
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

fn valid_slug(value: &str) -> bool {
    let length = value.len();
    if length == 0 || length > 80 {
        return false;
    }
    let mut characters = value.chars();
    let first = characters.next().unwrap();
    let last = value.chars().last().unwrap();
    let middle = value.chars().all(|character| {
        character.is_ascii_lowercase() || character.is_ascii_digit() || character == '-'
    });
    let ends = first.is_ascii_lowercase() || first.is_ascii_digit();
    let last_ok = last.is_ascii_lowercase() || last.is_ascii_digit();
    middle && ends && last_ok
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
            tracing::error!(%message, "rust knowledge api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn parse_object(body: &Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err(legacy_error(
            "Request body must be a JSON object.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        )),
    }
}

fn legacy_error(message: &str, status: StatusCode, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
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
    tracing::error!(%error, "rust knowledge api transaction failed");
    unavailable()
}

fn database_error(message: String) -> Response {
    tracing::error!(%message, "rust knowledge api database operation failed");
    unavailable()
}
