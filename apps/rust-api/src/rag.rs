use std::time::Duration;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};
use uuid::Uuid;

use crate::proxy::AppState;
use crate::tenancy::is_postgres;

pub struct IngestResult {
    pub chunks: i64,
    pub embedding_model: String,
    pub collection: String,
}

fn chunk_text(text: &str, chunk_size: usize, overlap: usize) -> Result<Vec<String>, String> {
    if text.trim().is_empty() {
        return Err("Document text must be non-empty.".into());
    }
    if !(200..=8000).contains(&chunk_size) {
        return Err("Chunk size must be between 200 and 8000 characters.".into());
    }
    if overlap >= chunk_size {
        return Err("Chunk overlap must be smaller than chunk size.".into());
    }
    let normalized = text
        .lines()
        .map(str::trim_end)
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string();
    let chars: Vec<char> = normalized.chars().collect();
    let mut chunks = Vec::new();
    let step = chunk_size - overlap;
    let mut index = 0;
    while index < chars.len() {
        let end = (index + chunk_size).min(chars.len());
        let content: String = chars[index..end].iter().collect();
        let trimmed = content.trim();
        if !trimmed.is_empty() {
            chunks.push(trimmed.to_string());
        }
        if index + chunk_size >= chars.len() {
            break;
        }
        index += step;
    }
    Ok(chunks)
}

pub async fn ingest_document(
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
    document_id: &str,
) -> Result<IngestResult, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "Database is unavailable.".to_string())?;
    mark_ingesting(pool, state, document_id).await?;
    match ingest_inner(pool, state, tenant_id, project_id, user_id, document_id).await {
        Ok(result) => {
            mark_ready(pool, state, document_id, result.chunks).await?;
            Ok(result)
        }
        Err(error) => {
            mark_failed(pool, state, document_id, &error).await?;
            Err(error)
        }
    }
}

async fn ingest_inner(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
    document_id: &str,
) -> Result<IngestResult, String> {
    let postgres = is_postgres(state);
    let metadata_text = if postgres {
        "CAST(document.metadata AS TEXT)"
    } else {
        "document.metadata"
    };
    let sql = format!(
        "SELECT document.content, document.title, {metadata_text} AS metadata_text, \
         CAST(base.id AS TEXT) AS knowledge_base_id, base.chunk_size, base.chunk_overlap, \
         CAST(base.is_archived AS INTEGER) AS is_archived, CAST(base.tenant_id AS TEXT) AS tenant_id, CAST(base.project_id AS TEXT) AS project_id, \
         CAST(base.embedding_model_id AS TEXT) AS embedding_model_id, base.name AS base_name \
         FROM core_knowledgedocument document JOIN core_knowledgebase base \
           ON CAST(base.id AS TEXT) = CAST(document.knowledge_base_id AS TEXT) \
         WHERE CAST(document.id AS TEXT) = ?"
    );
    let row = sqlx::query(&sql)
        .bind(document_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "Knowledge document is unavailable.".to_string())?;
    if row.try_get::<String, _>("tenant_id").unwrap_or_default() != tenant_id
        || row.try_get::<String, _>("project_id").unwrap_or_default() != project_id
    {
        return Err("Knowledge document is outside the task scope.".into());
    }
    if row.try_get::<i64, _>("is_archived").unwrap_or(1) != 0 {
        return Err("Knowledge base is archived.".into());
    }
    let content: String = row.try_get("content").unwrap_or_default();
    let chunk_size = row.try_get::<i64, _>("chunk_size").unwrap_or(1200) as usize;
    let overlap = row.try_get::<i64, _>("chunk_overlap").unwrap_or(160) as usize;
    let model_id: String = row.try_get("embedding_model_id").unwrap_or_default();
    let model = crate::runtime_model::load(pool, state, tenant_id, user_id, &model_id)
        .await?
        .ok_or_else(|| "Embedding model is unavailable.".to_string())?;
    if !model.capabilities.iter().any(|value| value == "embeddings") {
        return Err("Selected model does not declare embeddings.".into());
    }
    let credential = crate::runtime_model::credential(pool, state, &model, user_id).await?;
    let metadata: Value = row
        .try_get::<Option<String>, _>("metadata_text")
        .ok()
        .flatten()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({}));
    let chunks = chunk_text(&content, chunk_size, overlap)?;
    let mut store = QdrantStore::from_env()?;
    let mut inserted = 0_i64;
    for (batch_index, batch) in chunks.chunks(128).enumerate() {
        let scope = format!("rag-ingest:{document_id}");
        let key = format!("rag-ingest:{document_id}:{}", batch_index * 128);
        let vectors =
            embed_guarded(state, &model, &credential, batch, tenant_id, &scope, &key).await?;
        if batch_index == 0 {
            store = store.for_model(&model.model_name, vectors[0].len());
            store.ensure_collection(vectors[0].len()).await?;
        }
        let mut points = Vec::with_capacity(batch.len());
        for (offset, (text, vector)) in batch.iter().zip(vectors).enumerate() {
            let chunk_index = batch_index * 128 + offset;
            let digest = hex::encode(Sha256::digest(text.as_bytes()));
            let chunk_id = format!("{document_id}:{chunk_index}:{}", &digest[..16]);
            points.push(json!({
                "id": Uuid::new_v5(
                    &Uuid::NAMESPACE_URL,
                    format!("{tenant_id}:{project_id}:{chunk_id}").as_bytes()
                ).to_string(),
                "vector": vector,
                "payload": {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "chunk_id": chunk_id,
                    "text": text,
                    "metadata": {
                        "document_id": document_id,
                        "chunk_index": chunk_index,
                        "embedding_model": model.model_name,
                        "knowledge_base_id": row.try_get::<String, _>("knowledge_base_id").unwrap_or_default(),
                        "title": row.try_get::<String, _>("title").unwrap_or_default(),
                    },
                },
            }));
            inserted += 1;
        }
        store.upsert(tenant_id, project_id, points).await?;
    }
    let _ = metadata;
    Ok(IngestResult {
        chunks: inserted,
        embedding_model: model.model_name,
        collection: store.collection,
    })
}

async fn mark_ingesting(pool: &AnyPool, state: &AppState, document_id: &str) -> Result<(), String> {
    let postgres = is_postgres(state);
    let sql = if postgres {
        "UPDATE core_knowledgedocument SET status = 'ingesting', error = NULL, updated_at = now() \
         WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_knowledgedocument SET status = 'ingesting', error = NULL, updated_at = datetime('now') \
         WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(document_id)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

async fn mark_ready(
    pool: &AnyPool,
    state: &AppState,
    document_id: &str,
    chunks: i64,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let sql = if postgres {
        "UPDATE core_knowledgedocument SET status = 'ready', chunk_count = ?, error = NULL, \
         ingested_at = now(), updated_at = now() WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_knowledgedocument SET status = 'ready', chunk_count = ?, error = NULL, \
         ingested_at = datetime('now'), updated_at = datetime('now') WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(chunks)
        .bind(document_id)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

async fn mark_failed(
    pool: &AnyPool,
    state: &AppState,
    document_id: &str,
    error: &str,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let payload = json!({"code": "KNOWLEDGE_INGEST_FAILED", "message": error}).to_string();
    let sql = if postgres {
        "UPDATE core_knowledgedocument SET status = 'failed', error = ?::jsonb, \
         updated_at = now() WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_knowledgedocument SET status = 'failed', error = ?, \
         updated_at = datetime('now') WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(payload)
        .bind(document_id)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

async fn embed_guarded(
    state: &AppState,
    model: &crate::runtime_model::RuntimeModel,
    credential: &str,
    inputs: &[String],
    tenant_id: &str,
    scope_ref: &str,
    idempotency_key: &str,
) -> Result<Vec<Vec<f64>>, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "Database is unavailable.".to_string())?;
    let requested = (inputs.iter().map(|value| value.len() as i64).sum::<i64>() / 2).max(1);
    let reservation = crate::token_guard::reserve_adhoc(
        pool,
        is_postgres(state),
        crate::token_guard::AdHocRequest {
            tenant_id,
            scope_ref,
            idempotency_key,
            requested_tokens: requested,
        },
    )
    .await?;
    match model.embed(state, credential, inputs).await {
        Ok(vectors) => {
            crate::token_guard::settle(pool, &reservation, reservation.reserved_tokens).await?;
            Ok(vectors)
        }
        Err(error) => {
            let _ = crate::token_guard::release(pool, &reservation).await;
            Err(error)
        }
    }
}

pub async fn retrieve(
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
    knowledge_base_id: &str,
    query: &str,
    limit: i64,
) -> Result<Vec<Value>, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "Database is unavailable.".to_string())?;
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let sql = format!(
        "SELECT CAST(base.embedding_model_id AS TEXT) AS embedding_model_id, base.is_archived, \
         CAST(base.tenant_id AS TEXT) AS tenant_id, CAST(base.project_id AS TEXT) AS project_id \
         FROM core_knowledgebase base WHERE CAST(base.id AS TEXT) = ? AND base.is_archived = {true_literal}"
    );
    let row = sqlx::query(&sql)
        .bind(knowledge_base_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "Knowledge base is unavailable.".to_string())?;
    if row.try_get::<String, _>("tenant_id").unwrap_or_default() != tenant_id
        || row.try_get::<String, _>("project_id").unwrap_or_default() != project_id
    {
        return Err("Knowledge base is outside the project scope.".into());
    }
    let model = crate::runtime_model::load(
        pool,
        state,
        tenant_id,
        user_id,
        &row.try_get::<String, _>("embedding_model_id")
            .unwrap_or_default(),
    )
    .await?
    .ok_or_else(|| "Knowledge embedding model is unavailable.".to_string())?;
    let credential = crate::runtime_model::credential(pool, state, &model, user_id).await?;
    let scope = format!("rag-retrieve:{project_id}");
    let digest = {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(query.as_bytes());
        format!("{:x}", hasher.finalize())[..16].to_string()
    };
    let key = format!("rag-retrieve:{project_id}:{digest}");
    let vectors = embed_guarded(
        state,
        &model,
        &credential,
        &[query.to_string()],
        tenant_id,
        &scope,
        &key,
    )
    .await?;
    let store = QdrantStore::from_env()?.for_model(&model.model_name, vectors[0].len());
    let filters = json!({
        "tenant_id": tenant_id,
        "project_id": project_id,
        "metadata.knowledge_base_id": knowledge_base_id,
    });
    let hits = store
        .search(
            tenant_id,
            project_id,
            &vectors[0],
            limit.clamp(1, 50),
            &filters,
        )
        .await?;
    Ok(hits
        .into_iter()
        .map(|hit| {
            let payload = hit.get("payload").cloned().unwrap_or_else(|| json!({}));
            let metadata = payload
                .get("metadata")
                .cloned()
                .unwrap_or_else(|| json!({}));
            json!({
                "text": payload.get("text").and_then(Value::as_str).unwrap_or(""),
                "score": hit.get("score").cloned().unwrap_or(Value::Null),
                "document_id": metadata.get("document_id").cloned().unwrap_or(Value::Null),
                "chunk_id": payload.get("chunk_id").cloned().unwrap_or(Value::Null),
                "knowledge_base_id": knowledge_base_id,
            })
        })
        .collect())
}

struct QdrantStore {
    base_url: String,
    collection: String,
    api_key: String,
    client: reqwest::Client,
}

impl QdrantStore {
    fn from_env() -> Result<Self, String> {
        let base_url = std::env::var("QDRANT_URL")
            .unwrap_or_default()
            .trim_end_matches('/')
            .to_string();
        if !(base_url.starts_with("http://") || base_url.starts_with("https://")) {
            return Err("QDRANT_URL must use HTTP or HTTPS.".into());
        }
        let collection =
            std::env::var("QDRANT_COLLECTION").unwrap_or_else(|_| "agent_knowledge".into());
        if collection.is_empty() || collection.len() > 128 {
            return Err("QDRANT_COLLECTION is invalid.".into());
        }
        let reference = std::env::var("QDRANT_API_KEY_REF").unwrap_or_default();
        let api_key = if reference.is_empty() {
            String::new()
        } else {
            std::env::var(reference.trim_start_matches("env:"))
                .map_err(|_| "Qdrant API key is not configured.".to_string())?
        };
        Ok(Self {
            base_url,
            collection,
            api_key,
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(15))
                .build()
                .map_err(|error| error.to_string())?,
        })
    }

    fn for_model(&self, model_name: &str, dimensions: usize) -> Self {
        let slug = model_name
            .to_lowercase()
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
            .collect::<String>()
            .trim_matches('-')
            .to_string();
        let slug = slug.chars().take(40).collect::<String>();
        Self {
            base_url: self.base_url.clone(),
            collection: format!("{}__{}-{}", self.collection, slug, dimensions),
            api_key: self.api_key.clone(),
            client: self.client.clone(),
        }
    }

    async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, String> {
        let mut request = self
            .client
            .request(method, format!("{}{}", self.base_url, path))
            .header("Accept", "application/json");
        if !self.api_key.is_empty() {
            request = request.header("api-key", &self.api_key);
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|_| "Vector database is unavailable.".to_string())?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(Value::Null);
        }
        if !response.status().is_success() {
            return Err(format!(
                "Vector database returned HTTP {}.",
                response.status().as_u16()
            ));
        }
        if response.status() == reqwest::StatusCode::NO_CONTENT {
            return Ok(json!({}));
        }
        response
            .json::<Value>()
            .await
            .map_err(|_| "Vector database returned invalid JSON.".to_string())
    }

    async fn ensure_collection(&self, dimensions: usize) -> Result<(), String> {
        let path = format!("/collections/{}", self.collection);
        let existing = self.request(reqwest::Method::GET, &path, None).await?;
        if !existing.is_null() {
            return Ok(());
        }
        self.request(
            reqwest::Method::PUT,
            &path,
            Some(json!({"vectors": {"size": dimensions, "distance": "Cosine"}})),
        )
        .await?;
        Ok(())
    }

    async fn upsert(
        &self,
        tenant_id: &str,
        project_id: &str,
        points: Vec<Value>,
    ) -> Result<(), String> {
        if points.is_empty() {
            return Ok(());
        }
        let mut points = points;
        for point in &mut points {
            if let Some(payload) = point.get_mut("payload").and_then(Value::as_object_mut) {
                payload.insert("tenant_id".into(), json!(tenant_id));
                payload.insert("project_id".into(), json!(project_id));
            }
        }
        self.request(
            reqwest::Method::PUT,
            &format!("/collections/{}/points?wait=true", self.collection),
            Some(json!({"points": points})),
        )
        .await?;
        Ok(())
    }

    async fn search(
        &self,
        tenant_id: &str,
        project_id: &str,
        vector: &[f64],
        limit: i64,
        metadata_filters: &Value,
    ) -> Result<Vec<Value>, String> {
        let mut must = vec![
            json!({"key": "tenant_id", "match": {"value": tenant_id}}),
            json!({"key": "project_id", "match": {"value": project_id}}),
        ];
        if let Some(filters) = metadata_filters.as_object() {
            for (key, value) in filters {
                if key == "tenant_id" || key == "project_id" {
                    continue;
                }
                must.push(json!({"key": key, "match": {"value": value}}));
            }
        }
        let body = json!({
            "vector": vector,
            "limit": limit,
            "with_payload": true,
            "filter": {"must": must},
        });
        let payload = self
            .request(
                reqwest::Method::POST,
                &format!("/collections/{}/points/search", self.collection),
                Some(body),
            )
            .await?;
        Ok(payload
            .get("result")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }
}
