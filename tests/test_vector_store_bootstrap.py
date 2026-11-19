import pytest
from core import rag_runtime
from core.vector_store import QdrantVectorStore, VectorStoreError
from rest_framework.test import APIClient


def _store(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", "agent_knowledge")
    store = QdrantVectorStore()
    calls = []

    def fake_request(method, path, body=None, allow_missing=False):
        calls.append((method, path, body, allow_missing))
        return None

    monkeypatch.setattr(QdrantVectorStore, "_request", staticmethod(fake_request))
    return store, calls


def test_ensure_collection_creates_the_collection_when_missing(monkeypatch):
    store, calls = _store(monkeypatch)

    assert store.ensure_collection(768) == {"created": True}
    assert calls[0][:2] == ("GET", "/collections/agent_knowledge")
    assert calls[0][3] is True
    assert calls[1][:3] == (
        "PUT",
        "/collections/agent_knowledge",
        {"vectors": {"size": 768, "distance": "Cosine"}},
    )


def test_ensure_collection_is_idempotent_and_validates_input(monkeypatch):
    store, calls = _store(monkeypatch)
    monkeypatch.setattr(
        QdrantVectorStore,
        "_request",
        staticmethod(lambda method, path, body=None, allow_missing=False: {"result": {}}),
    )

    assert store.ensure_collection(768) == {"created": False}
    with pytest.raises(VectorStoreError, match="positive integer"):
        store.ensure_collection(0)
    with pytest.raises(VectorStoreError, match="distance"):
        store.ensure_collection(768, distance="Hamming")


def test_ping_reports_missing_and_unhealthy_collections(monkeypatch):
    store, _ = _store(monkeypatch)
    with pytest.raises(VectorStoreError, match="missing"):
        store.ping()

    monkeypatch.setattr(
        QdrantVectorStore,
        "_request",
        staticmethod(lambda method, path, body=None, allow_missing=False: {"status": "error"}),
    )
    assert store.ping() is False


def test_ingestion_creates_the_collection_once(monkeypatch):
    class FakeGateway:
        def embed(self, model_config, inputs):
            return type("Result", (), {"vectors": [[0.1, 0.2, 0.3] for _ in inputs]})()

    class FakeStore:
        def __init__(self):
            self.collection_calls = []
            self.upsert_calls = 0

        def ensure_collection(self, vector_size):
            self.collection_calls.append(vector_size)
            return {"created": True}

        def upsert(self, tenant_id, project_id, records):
            self.upsert_calls += 1
            return {"upserted": len(records)}

    store = FakeStore()
    monkeypatch.setattr(rag_runtime, "_validate_scope", lambda *args: None)
    monkeypatch.setattr(rag_runtime, "ModelGateway", lambda: FakeGateway())

    result = rag_runtime.ingest_text(
        tenant_id="tenant-1",
        project_id="project-1",
        document_id="doc-1",
        text="a" * 6000,
        embedding_model=type("Model", (), {"model_name": "embed-model"})(),
        vector_store=store,
    )

    assert store.collection_calls == [3]
    assert store.upsert_calls == 1
    assert result["chunks"] > 1


@pytest.mark.django_db
def test_readiness_skips_the_vector_probe_when_unconfigured(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    response = APIClient().get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "vector_store": "not_configured",
        "cache": "disabled",
    }


@pytest.mark.django_db
def test_readiness_fails_when_the_vector_store_is_unavailable(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")

    def failing_ping(self):
        raise VectorStoreError("Vector database is unavailable.")

    monkeypatch.setattr(QdrantVectorStore, "ping", failing_ping)
    response = APIClient().get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "ok",
        "vector_store": "unavailable",
        "cache": "disabled",
    }


@pytest.mark.django_db
def test_readiness_reports_a_healthy_vector_store(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setattr(QdrantVectorStore, "ping", lambda self: True)
    response = APIClient().get("/health/ready")
    assert response.status_code == 200
    assert response.json()["vector_store"] == "ok"


def test_collection_for_model_versions_collections_by_model_and_dimension(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", "agent_knowledge")
    store = QdrantVectorStore()

    name = store.collection_for_model("BAAI/bge-m3:large", 1024)
    assert name == "agent_knowledge__baai-bge-m3-large-1024"

    different = store.collection_for_model("text-embedding-3-small", 1024)
    assert different != name
    with pytest.raises(VectorStoreError, match="positive integer"):
        store.collection_for_model("bge-m3", 0)
    with pytest.raises(VectorStoreError, match="collection"):
        store.collection_for_model("///", 768)


def test_for_model_binds_a_copy_to_the_derived_collection(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", "agent_knowledge")
    monkeypatch.setenv("QDRANT_API_KEY_REF", "env:QDRANT_TEST_KEY")
    monkeypatch.setenv("QDRANT_TEST_KEY", "secret-key")
    store = QdrantVectorStore()

    scoped = store.for_model("bge-m3", 768)

    assert scoped.collection == "agent_knowledge__bge-m3-768"
    assert scoped.collection != store.collection
    assert scoped.base_url == store.base_url
    assert scoped.api_key == "secret-key"
