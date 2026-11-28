//! Knowledge maintenance: re-embedding documents and pruning collections.
//!
//! Both operations are explicit and auditable: reindexing never touches a
//! legacy collection, and pruning never deletes a collection a document still
//! references or one outside the configured collection namespace.

use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

const MAX_LIMIT: i64 = 5000;

fn base_prefix() -> Result<String, String> {
    let collection =
        std::env::var("QDRANT_COLLECTION").unwrap_or_else(|_| "agent_knowledge".into());
    if collection.is_empty() || collection.len() > 128 {
        return Err("QDRANT_COLLECTION is invalid.".into());
    }
    Ok(format!("{collection}__"))
}

fn valid_collection_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 200
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' || ch == '.')
}

/// Report, or delete, collections no knowledge document references.
pub async fn prune_qdrant_collections(
    state: &AppState,
    apply: bool,
    prefix: Option<String>,
) -> Result<Value, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "database unavailable".to_string())?;
    let base = base_prefix()?;
    let prefix = prefix
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| base.clone());
    if !prefix.starts_with(&base) {
        return Err(format!(
            "Refusing to prune outside the configured collection namespace ({base})."
        ));
    }
    if !valid_collection_name(prefix.trim_end_matches('_')) {
        return Err("Invalid collection prefix.".to_string());
    }
    let active: Vec<String> = sqlx::query_scalar::<_, String>(
        "SELECT DISTINCT vector_collection FROM core_knowledgedocument \
         WHERE vector_collection IS NOT NULL AND vector_collection <> ''",
    )
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    let active_set: std::collections::HashSet<String> = active.into_iter().collect();
    let collections = crate::rag::list_collections().await?;
    let candidates: Vec<String> = collections
        .iter()
        .filter(|name| name.starts_with(&prefix) && !active_set.contains(*name))
        .cloned()
        .collect();
    let mut deleted = 0usize;
    let mut failures = Vec::new();
    if apply {
        for name in &candidates {
            match crate::rag::delete_collection(name).await {
                Ok(()) => deleted += 1,
                Err(error) => failures.push(json!({"collection": name, "error": error})),
            }
        }
    }
    Ok(json!({
        "total": collections.len(),
        "active": active_set.len(),
        "candidates": candidates,
        "deleted": deleted,
        "failures": failures,
        "mode": if apply { "apply" } else { "dry-run" },
    }))
}

/// Re-embed documents into the collection owned by their current embedding
/// model. One failing document never aborts the run: failures are collected
/// so an operator can retry only those documents.
pub async fn reindex_knowledge(
    state: &AppState,
    documents: Vec<String>,
    limit: i64,
    force: bool,
    dry_run: bool,
) -> Result<Value, String> {
    let pool: &AnyPool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "database unavailable".to_string())?;
    let limit = if limit <= 0 {
        MAX_LIMIT
    } else {
        limit.min(MAX_LIMIT)
    } as usize;
    let sql = "SELECT CAST(doc.id AS TEXT) AS id, doc.status, \
         COALESCE(doc.vector_collection, '') AS vector_collection, \
         CAST(base.tenant_id AS TEXT) AS tenant_id, CAST(base.project_id AS TEXT) AS project_id, \
         doc.uploaded_by_id, model.model_name \
         FROM core_knowledgedocument doc \
         JOIN core_knowledgebase base ON CAST(base.id AS TEXT) = CAST(doc.knowledge_base_id AS TEXT) \
         JOIN core_modelconfig model ON CAST(model.id AS TEXT) = CAST(base.embedding_model_id AS TEXT) \
         ORDER BY doc.created_at ASC, CAST(doc.id AS TEXT) ASC";
    let rows = sqlx::query(sql)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    let wanted: std::collections::HashSet<String> = documents.into_iter().collect();
    let mut summary = json!({
        "planned": [],
        "reindexed": [],
        "skipped_current": 0,
        "skipped_not_ready": 0,
        "failed": [],
    });
    let mut processed = 0usize;
    for row in rows {
        let document_id: String = row.try_get("id").unwrap_or_default();
        if !wanted.is_empty() && !wanted.contains(&document_id) {
            continue;
        }
        if wanted.is_empty() && processed >= limit {
            break;
        }
        let status: String = row.try_get("status").unwrap_or_default();
        if status != "ready" {
            bump(&mut summary, "skipped_not_ready");
            continue;
        }
        let current: String = row.try_get("vector_collection").unwrap_or_default();
        let model_name: String = row.try_get("model_name").unwrap_or_default();
        let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
        let project_id: String = row.try_get("project_id").unwrap_or_default();
        let user_id: i64 = row.try_get("uploaded_by_id").unwrap_or(0);
        let needs = match force {
            true => true,
            false => match crate::rag::collection_prefix_for_model(&model_name) {
                Ok(prefix) => !current.starts_with(&prefix),
                Err(error) => {
                    push_failed(&mut summary, &document_id, &error);
                    continue;
                }
            },
        };
        if !needs {
            bump(&mut summary, "skipped_current");
            continue;
        }
        if let Some(list) = summary.get_mut("planned").and_then(Value::as_array_mut) {
            list.push(json!(document_id));
        }
        if dry_run {
            continue;
        }
        processed += 1;
        match crate::rag::ingest_document(state, &tenant_id, &project_id, user_id, &document_id)
            .await
        {
            Ok(result) => {
                if let Some(list) = summary.get_mut("reindexed").and_then(Value::as_array_mut) {
                    list.push(json!({
                        "document_id": document_id,
                        "collection": result.collection,
                        "chunks": result.chunks,
                        "embedding_model": result.embedding_model,
                    }));
                }
            }
            Err(error) => push_failed(&mut summary, &document_id, &error),
        }
    }
    Ok(summary)
}

fn bump(summary: &mut Value, key: &str) {
    if let Some(object) = summary.as_object_mut() {
        let value = object.get(key).and_then(Value::as_i64).unwrap_or(0);
        object.insert(key.to_string(), json!(value + 1));
    }
}

fn push_failed(summary: &mut Value, document_id: &str, error: &str) {
    if let Some(list) = summary.get_mut("failed").and_then(Value::as_array_mut) {
        list.push(json!({"document_id": document_id, "error": error}));
    }
}
