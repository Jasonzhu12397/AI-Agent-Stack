import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from core.models import ModelConfig, ModelProvider, Project, Tenant
from core.rag_runtime import ingest_text, retrieve_context
from core.vector_store import QdrantVectorStore
from django.contrib.auth import get_user_model
from django.test import override_settings

QDRANT_URL = os.getenv("QDRANT_LIVE_URL", "").strip()
requires_qdrant = pytest.mark.skipif(
    not QDRANT_URL,
    reason="Set QDRANT_LIVE_URL to run live Qdrant integration tests.",
)


class _EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inputs = body.get("input") or []
        payload = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": [1.0, 0.0, 0.0, 0.0]}
                for index, _text in enumerate(inputs)
            ],
            "model": body.get("model", "local-embedding"),
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


@pytest.fixture
def embedding_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@requires_qdrant
def test_live_qdrant_collection_lifecycle():
    store = QdrantVectorStore(base_url=QDRANT_URL, collection="agent_knowledge_live_contract")
    store.ping_service()
    store.ensure_collection(4)

    assert "agent_knowledge_live_contract" in store.list_collections()
    store.delete_collection("agent_knowledge_live_contract")
    assert "agent_knowledge_live_contract" not in store.list_collections()


@pytest.mark.django_db
@requires_qdrant
def test_live_rag_ingest_and_retrieve_against_qdrant(monkeypatch, embedding_server):
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("QDRANT_COLLECTION", "agent_knowledge_itest")
    user = get_user_model().objects.create_user(username="qdrant-live")
    tenant = Tenant.objects.create(name="Qdrant live", slug="qdrant-live")
    project = Project.objects.create(tenant=tenant, name="Live RAG", slug="live-rag")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Local embedding",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url=embedding_server,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="local-embedding",
        display_name="Local embedding",
        capabilities=["embeddings"],
    )

    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["127.0.0.1"]):
        ingested = ingest_text(
            tenant_id=str(tenant.id),
            project_id=str(project.id),
            document_id="qdrant-live-document",
            text="Bare metal provisioning must remain deterministic and auditable.",
            embedding_model=model,
        )
        results = retrieve_context(
            tenant_id=str(tenant.id),
            project_id=str(project.id),
            query="auditable bare metal provisioning",
            embedding_model=model,
            limit=3,
        )

    assert ingested["chunks"] == 1
    assert ingested["collection"] == "agent_knowledge_itest__local-embedding-4"
    assert results[0]["document_id"] == "qdrant-live-document"
    assert "Bare metal" in results[0]["text"]
    assert results[0]["score"] == pytest.approx(1.0, abs=1e-4)

    QdrantVectorStore(base_url=QDRANT_URL).delete_collection(ingested["collection"])
