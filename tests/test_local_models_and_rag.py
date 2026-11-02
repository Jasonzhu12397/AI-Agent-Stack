import json
from unittest.mock import patch

import pytest
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import ModelConfig, ModelProvider
from core.rag_runtime import RagRuntimeError, chunk_text
from core.vector_store import QdrantVectorStore, VectorStoreError
from django.test import override_settings


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return FakeHTTPResponse(self.payload)


def local_model(capabilities=None):
    provider = ModelProvider(
        name="Local Ollama",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="http://ollama:11434/v1",
        credential_ref="",
    )
    return ModelConfig(
        provider=provider,
        model_name="qwen2.5:14b",
        display_name="Qwen Local",
        capabilities=capabilities or ["chat", "embeddings"],
        max_output_tokens=1024,
    )


def test_local_openai_compatible_chat_uses_allowlisted_http_endpoint():
    opener = FakeOpener(
        {
            "choices": [{"message": {"content": "local answer"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        }
    )
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        with patch("core.model_gateway.build_opener", return_value=opener):
            result = ModelGateway().complete(
                local_model(),
                [{"role": "user", "content": "hello"}],
            )

    assert result.content == "local answer"
    assert opener.request.full_url == (
        "http://ollama:11434/v1/chat/completions"
    )
    assert opener.request.get_header("Authorization") is None


def test_local_openai_compatible_chat_rejects_non_allowlisted_http_endpoint():
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=[]):
        with pytest.raises(ModelGatewayError) as exc:
            ModelGateway().complete(
                local_model(),
                [{"role": "user", "content": "hello"}],
            )

    assert exc.value.code == "LOCAL_MODEL_HOST_NOT_ALLOWED"


def test_embedding_gateway_normalizes_vectors_and_sends_model_name():
    opener = FakeOpener(
        {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
    )
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        with patch("core.model_gateway.build_opener", return_value=opener):
            result = ModelGateway().embed(local_model(), ["first", "second"])

    assert result.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert opener.request.full_url == "http://ollama:11434/v1/embeddings"
    body = json.loads(opener.request.data.decode("utf-8"))
    assert body["model"] == "qwen2.5:14b"
    assert body["input"] == ["first", "second"]


def test_embedding_gateway_rejects_model_without_embedding_capability():
    with pytest.raises(ModelGatewayError) as exc:
        ModelGateway().embed(local_model(["chat"]), ["hello"])

    assert exc.value.code == "MODEL_CAPABILITY_MISSING"


def test_embedding_gateway_rejects_non_finite_vector_values():
    opener = FakeOpener({"data": [{"index": 0, "embedding": [float("nan")]}]})
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        with patch("core.model_gateway.build_opener", return_value=opener):
            with pytest.raises(ModelGatewayError) as exc:
                ModelGateway().embed(local_model(), ["hello"])

    assert exc.value.code == "MODEL_PROVIDER_INVALID_EMBEDDING"


def test_chunk_text_applies_overlap_and_validates_parameters():
    text = "a" * 500
    chunks = chunk_text(text, chunk_size=300, overlap=50)

    assert len(chunks) == 2
    assert chunks[0][-50:] == chunks[1][:50]


def test_chunk_text_rejects_invalid_overlap():
    with pytest.raises(RagRuntimeError):
        chunk_text("document", chunk_size=200, overlap=200)


def test_qdrant_search_always_filters_tenant_and_project(monkeypatch):
    store = QdrantVectorStore(base_url="http://qdrant:6333")
    captured = {}

    def fake_request(method, path, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return {"result": []}

    monkeypatch.setattr(store, "_request", fake_request)
    result = store.search("tenant-a", "project-b", [0.1, 0.2], limit=4)

    assert result == []
    assert captured["body"]["filter"]["must"] == [
        {"key": "tenant_id", "match": {"value": "tenant-a"}},
        {"key": "project_id", "match": {"value": "project-b"}},
    ]


def test_qdrant_search_requires_tenant_and_project_scope():
    store = QdrantVectorStore(base_url="http://qdrant:6333")

    with pytest.raises(VectorStoreError):
        store.search("", "project-b", [0.1, 0.2])


def test_qdrant_upsert_generates_stable_tenant_scoped_point_ids(monkeypatch):
    store = QdrantVectorStore(base_url="http://qdrant:6333")
    captured = {}

    def fake_request(method, path, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return {"result": {"status": "completed"}}

    monkeypatch.setattr(store, "_request", fake_request)
    result = store.upsert(
        "tenant-a",
        "project-b",
        [{"chunk_id": "doc:0", "text": "hello", "vector": [0.1, 0.2]}],
    )

    point = captured["body"]["points"][0]
    assert result == {"upserted": 1}
    assert point["payload"]["tenant_id"] == "tenant-a"
    assert point["payload"]["project_id"] == "project-b"
    assert len(point["id"]) == 36


@pytest.mark.django_db
def test_local_model_provider_can_be_configured_without_api_key():
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient
    from core.models import Tenant, TenantMembership

    user = get_user_model().objects.create_user(
        username="local-model-admin",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Local model tenant", slug="local-model-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    client = APIClient()
    client.force_authenticate(user=user)

    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/model-providers/",
            {
                "name": "Ollama local",
                "provider_type": "openai_compatible",
                "base_url": "http://ollama:11434/v1",
                "credential_ref": "",
                "config": {},
            },
            format="json",
        )

    assert response.status_code == 201
    assert response.data["base_url"] == "http://ollama:11434/v1"
    assert response.data["credential_configured"] is False
