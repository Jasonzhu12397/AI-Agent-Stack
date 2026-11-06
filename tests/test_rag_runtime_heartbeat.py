from types import SimpleNamespace

import pytest

from core import rag_runtime


class FakeGateway:
    def __init__(self):
        self.batch_sizes = []

    def embed(self, model_config, texts):
        self.batch_sizes.append(len(texts))
        return SimpleNamespace(vectors=[[0.1, 0.2] for _ in texts])


class FakeVectorStore:
    def __init__(self):
        self.upsert_calls = []

    def upsert(self, tenant_id, project_id, records):
        self.upsert_calls.append((tenant_id, project_id, records))
        return {"stored": len(records)}


def test_ingestion_renews_lease_before_each_batch_and_vector_write(monkeypatch):
    gateway = FakeGateway()
    store = FakeVectorStore()
    heartbeats = []
    model = SimpleNamespace(model_name="test-embedding-model")

    monkeypatch.setattr(rag_runtime, "_validate_scope", lambda *args: None)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda: gateway)

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
    assert len(store.upsert_calls) == 1
    assert result["chunks"] == 129
    assert result["stored"] == 129


def test_ingestion_stops_when_lease_heartbeat_fails(monkeypatch):
    gateway = FakeGateway()
    store = FakeVectorStore()
    model = SimpleNamespace(model_name="test-embedding-model")

    monkeypatch.setattr(rag_runtime, "_validate_scope", lambda *args: None)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda: gateway)

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
    assert store.upsert_calls == []


def test_ingestion_rejects_non_callable_heartbeat(monkeypatch):
    monkeypatch.setattr(rag_runtime, "_validate_scope", lambda *args: None)

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
