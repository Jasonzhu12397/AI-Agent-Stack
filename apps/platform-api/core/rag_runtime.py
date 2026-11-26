import hashlib

from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import Project
from core.token_guard import AdHocTokenGuard
from core.vector_store import QdrantVectorStore, VectorStoreError


class RagRuntimeError(Exception):
    pass


def chunk_text(text, chunk_size=1200, overlap=160):
    if not isinstance(text, str) or not text.strip():
        raise RagRuntimeError("Document text must be a non-empty string.")
    if not isinstance(chunk_size, int) or not 200 <= chunk_size <= 8000:
        raise RagRuntimeError("Chunk size must be between 200 and 8000 characters.")
    if not isinstance(overlap, int) or overlap < 0 or overlap >= chunk_size:
        raise RagRuntimeError("Chunk overlap must be smaller than chunk size.")
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    chunks = []
    for index in range(0, len(normalized), chunk_size - overlap):
        content = normalized[index : index + chunk_size].strip()
        if content:
            chunks.append(content)
        if index + chunk_size >= len(normalized):
            break
    return chunks


def _validate_scope(tenant_id, project_id, model_config):
    project = Project.objects.filter(pk=project_id, tenant_id=tenant_id).first()
    if project is None:
        raise RagRuntimeError("Project does not belong to the requested tenant.")
    provider_tenant_id = model_config.provider.tenant_id
    if provider_tenant_id not in (None, project.tenant_id):
        raise RagRuntimeError("Embedding model provider is outside the tenant scope.")
    if not model_config.enabled or not model_config.provider.enabled:
        raise RagRuntimeError("Embedding model is disabled.")
    if "embeddings" not in (model_config.capabilities or []):
        raise RagRuntimeError("Selected model does not have the embeddings capability.")
    return project


def ingest_text(
    *,
    tenant_id,
    project_id,
    document_id,
    text,
    embedding_model,
    vector_store=None,
    metadata=None,
    chunk_size=1200,
    overlap=160,
    heartbeat=None,
):
    project = _validate_scope(tenant_id, project_id, embedding_model)
    if heartbeat is not None and not callable(heartbeat):
        raise RagRuntimeError("Heartbeat must be callable when provided.")
    if not isinstance(document_id, str) or not document_id.strip():
        raise RagRuntimeError("Document ID is required.")
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    store = vector_store or QdrantVectorStore()
    records = []
    collection_ready = False
    collection = None
    guard = AdHocTokenGuard(
        tenant=project.tenant,
        scope_ref=f"rag-ingest:{document_id}",
    )
    gateway = ModelGateway(guard=guard)
    try:
        for batch_start in range(0, len(chunks), 128):
            if heartbeat is not None:
                heartbeat()
            batch = chunks[batch_start : batch_start + 128]
            embedded = gateway.embed(
                embedding_model,
                batch,
                idempotency_key=f"rag-ingest:{document_id}:{batch_start}",
            )
            if not collection_ready:
                dimension = len(embedded.vectors[0])
                binder = getattr(store, "for_model", None)
                if callable(binder):
                    store = binder(embedding_model.model_name, dimension)
                store.ensure_collection(dimension)
                collection = getattr(store, "collection", None)
                collection_ready = True
            for offset, (content, vector) in enumerate(zip(batch, embedded.vectors, strict=True)):
                chunk_index = batch_start + offset
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                records.append(
                    {
                        "chunk_id": f"{document_id}:{chunk_index}:{digest}",
                        "text": content,
                        "vector": vector,
                        "metadata": {
                            **(metadata or {}),
                            "document_id": document_id,
                            "chunk_index": chunk_index,
                            "embedding_model": embedding_model.model_name,
                        },
                    }
                )
        if heartbeat is not None:
            heartbeat()
        result = store.upsert(tenant_id, project_id, records)
    except (ModelGatewayError, VectorStoreError) as exc:
        raise RagRuntimeError(str(exc)) from exc
    return {
        "document_id": document_id,
        "chunks": len(records),
        "embedding_model": embedding_model.model_name,
        "collection": collection,
        "dimensions": len(records[0]["vector"]) if records else None,
        **result,
    }


def retrieve_context(
    *,
    tenant_id,
    project_id,
    query,
    embedding_model,
    vector_store=None,
    limit=5,
    score_threshold=None,
    metadata_filters=None,
):
    project = _validate_scope(tenant_id, project_id, embedding_model)
    if not isinstance(query, str) or not query.strip() or len(query) > 20000:
        raise RagRuntimeError("Query must be a non-empty string up to 20,000 characters.")
    store = vector_store or QdrantVectorStore()
    query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    guard = AdHocTokenGuard(
        tenant=project.tenant,
        scope_ref=f"rag-retrieve:{project_id}",
    )
    try:
        embedded = ModelGateway(guard=guard).embed(
            embedding_model,
            [query],
            idempotency_key=f"rag-retrieve:{project_id}:{query_digest}",
        )
        vector = embedded.vectors[0]
        binder = getattr(store, "for_model", None)
        if callable(binder):
            store = binder(embedding_model.model_name, len(vector))
        search_options = {
            "limit": limit,
            "score_threshold": score_threshold,
        }
        if metadata_filters is not None:
            search_options["metadata_filters"] = metadata_filters
        matches = store.search(
            tenant_id,
            project_id,
            vector,
            **search_options,
        )
    except (ModelGatewayError, VectorStoreError) as exc:
        raise RagRuntimeError(str(exc)) from exc
    return [
        {
            "text": item["payload"].get("text", ""),
            "score": item.get("score"),
            "document_id": (item["payload"].get("metadata") or {}).get("document_id"),
            "chunk_id": item["payload"].get("chunk_id"),
        }
        for item in matches
    ]
