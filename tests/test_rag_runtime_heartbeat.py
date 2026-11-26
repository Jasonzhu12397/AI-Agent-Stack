from types import SimpleNamespace

import pytest
from core import rag_runtime


def _fake_scope(*_args):
    return SimpleNamespace(tenant=SimpleNamespace(id="tenant-1"))


class FakeGateway:
    def __init__(self, guard=None):
        self.batch_sizes = []

    def embed(self, model_config, texts, **kwargs):
        self.batch_sizes.append(len(texts))
        return SimpleNamespace(vectors=[[0.1, 0.2] for _ in texts])


class FakeVectorStore:
    def __init__(self):
        self.upsert_calls = []
        self.collection_calls = []

    def ensure_collection(self, vector_size, distance="Cosine"):
        self.collection_calls.append((vector_size, distance))
        return {"created": True}

    def upsert(self, tenant_id, project_id, records):
        self.upsert_calls.append((tenant_id, project_id, records))
        return {"stored": len(records)}


def test_ingestion_renews_lease_before_each_batch_and_vector_write(monkeypatch):
    gateway = FakeGateway()
    store = FakeVectorStore()
    heartbeats = []
    model = SimpleNamespace(model_name="test-embedding-model")

    monkeypatch.setattr(rag_runtime, "_validate_scope", _fake_scope)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda guard=None: gateway)

    result = rag_runtime.ingest_text(
        tenant_id="tenant-1",
        project_id="project-1",
        document_id="document-1",
        text="x" * (200 * 129),
        embedding_model=model,
        vector_store=store,
        chunk_size=200,
        overlap=0,
        heartbeat=lambda: heartbeats.append("renewed"),
    )

    assert gateway.batch_sizes == [128, 1]
    assert len(heartbeats) == 3
    assert store.collection_calls == [(2, "Cosine")]
    assert len(store.upsert_calls) == 1
    assert result["chunks"] == 129
    assert result["stored"] == 129


def test_ingestion_stops_when_lease_heartbeat_fails(monkeypatch):
    gateway = FakeGateway()
    store = FakeVectorStore()
    model = SimpleNamespace(model_name="test-embedding-model")

    monkeypatch.setattr(rag_runtime, "_validate_scope", _fake_scope)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda guard=None: gateway)

    def fail_heartbeat():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        rag_runtime.ingest_text(
            tenant_id="tenant-1",
            project_id="project-1",
            document_id="document-2",
            text="x" * 200,
            embedding_model=model,
            vector_store=store,
            chunk_size=200,
            overlap=0,
            heartbeat=fail_heartbeat,
        )

    assert gateway.batch_sizes == []
    assert store.collection_calls == []
    assert store.upsert_calls == []


def test_ingestion_rejects_non_callable_heartbeat(monkeypatch):
    monkeypatch.setattr(rag_runtime, "_validate_scope", _fake_scope)

    with pytest.raises(rag_runtime.RagRuntimeError, match="Heartbeat must be callable"):
        rag_runtime.ingest_text(
            tenant_id="tenant-1",
            project_id="project-1",
            document_id="document-3",
            text="x" * 200,
            embedding_model=SimpleNamespace(model_name="test-embedding-model"),
            vector_store=FakeVectorStore(),
            chunk_size=200,
            overlap=0,
            heartbeat=True,
        )


def test_ingestion_and_retrieval_use_the_model_scoped_collection(monkeypatch):
    class ScopedStore:
        def __init__(self, collection, inner):
            self.collection = collection
            self.inner = inner
            self.ensure_calls = []

        def ensure_collection(self, vector_size, distance="Cosine"):
            self.ensure_calls.append(vector_size)
            return {"created": True}

        def upsert(self, tenant_id, project_id, records):
            self.inner.upsert(tenant_id, project_id, records)
            return {"stored": len(records)}

        def search(self, tenant_id, project_id, vector, limit=5, **kwargs):
            self.inner.search_vectors.append(vector)
            return []

    class Router:
        def __init__(self):
            self.bindings = []
            self.upsert_calls = []
            self.search_vectors = []
            self.base_collection = "agent_knowledge"

        def for_model(self, model_name, vector_size):
            binding = ScopedStore(f"{self.base_collection}__{model_name}-{vector_size}", self)
            self.bindings.append(binding)
            return binding

        def upsert(self, tenant_id, project_id, records):
            self.upsert_calls.append(records)

        def search(self, *args, **kwargs):
            raise AssertionError("retrieval must use the model scoped collection")

    gateway = FakeGateway()
    store = Router()
    model = SimpleNamespace(model_name="bge-m3")

    monkeypatch.setattr(rag_runtime, "_validate_scope", _fake_scope)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda guard=None: gateway)

    result = rag_runtime.ingest_text(
        tenant_id="tenant-1",
        project_id="project-1",
        document_id="document-4",
        text="x" * 200,
        embedding_model=model,
        vector_store=store,
        chunk_size=200,
        overlap=0,
    )
    assert result["collection"] == "agent_knowledge__bge-m3-2"
    assert result["dimensions"] == 2
    assert len(store.bindings) == 1
    assert store.bindings[0].ensure_calls == [2]

    rag_runtime.retrieve_context(
        tenant_id="tenant-1",
        project_id="project-1",
        query="edge",
        embedding_model=model,
        vector_store=store,
    )
    assert len(store.bindings) == 2
    assert store.bindings[1].collection == "agent_knowledge__bge-m3-2"
