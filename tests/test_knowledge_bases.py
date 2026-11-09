import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    KnowledgeBase,
    KnowledgeDocument,
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from worker_app import tasks as worker_tasks
from worker_app.tasks import execute_knowledge_ingest


@pytest.fixture
def knowledge_scope(db):
    user = get_user_model().objects.create_user(username="knowledge-admin")
    tenant = Tenant.objects.create(name="Knowledge tenant", slug="knowledge-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(
        tenant=tenant,
        name="Knowledge project",
        slug="knowledge-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Embedding provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="test-embedding-model",
        display_name="Test embedding model",
        capabilities=["embeddings"],
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {
        "user": user,
        "tenant": tenant,
        "project": project,
        "model": model,
        "client": client,
    }


@pytest.mark.django_db
def test_knowledge_base_document_api_queues_ingestion():
    user = get_user_model().objects.create_user(username="knowledge-admin")
    tenant = Tenant.objects.create(name="Knowledge tenant", slug="knowledge-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(
        tenant=tenant,
        name="Knowledge project",
        slug="knowledge-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Embedding provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="test-embedding-model",
        display_name="Test embedding model",
        capabilities=["embeddings"],
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/knowledge-bases/",
        {
            "name": "Operations knowledge",
            "slug": "operations",
            "embedding_model_id": str(model.id),
        },
        format="json",
    )
    assert response.status_code == 201
    document_response = client.post(
        f"/api/v1/knowledge-bases/{response.data['id']}/documents/",
        {
            "title": "Cluster runbook",
            "content": "Check API health, node readiness and CAPI conditions.",
            "metadata": {"source": "runbook"},
        },
        format="json",
    )
    assert document_response.status_code == 202
    assert "content" not in document_response.data
    document = KnowledgeDocument.objects.get(pk=document_response.data["id"])
    task = Task.objects.filter(task_type="knowledge.ingest").first()
    assert document.status == KnowledgeDocument.Status.PENDING
    assert task.tenant_id == tenant.id
    assert task.project_id == project.id



@pytest.mark.django_db
def test_knowledge_ingest_worker_marks_document_ready(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        embedding_model=scope["model"],
        name="Operations",
        slug="operations",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base,
        uploaded_by=scope["user"],
        title="Runbook",
        content="A runbook with cluster health and node readiness checks.",
        metadata={"source": "test"},
        checksum="a" * 64,
    )
    task = Task.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        task_type="knowledge.ingest",
        payload={"knowledge_document_id": str(document.id)},
        idempotency_key="knowledge-worker-ingest",
        available_at=timezone.now(),
    )
    with patch(
        "worker_app.tasks.ingest_text",
        return_value={"document_id": str(document.id), "chunks": 2, "upserted": 2},
    ) as ingest:
        result = execute_knowledge_ingest(task)
    document.refresh_from_db()
    assert result["chunks"] == 2
    assert document.status == KnowledgeDocument.Status.READY
    assert document.chunk_count == 2
    assert document.ingested_at is not None
    ingest.assert_called_once()



@pytest.mark.django_db
def test_agent_worker_injects_project_scoped_retrieved_context(knowledge_scope):
    scope = knowledge_scope
    chat_provider = ModelProvider.objects.create(
        tenant=scope["tenant"],
        created_by=scope["user"],
        name="Chat provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    chat_model = ModelConfig.objects.create(
        provider=chat_provider,
        model_name="test-chat-model",
        display_name="Test chat model",
        capabilities=["chat"],
    )
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        embedding_model=scope["model"],
        name="Runbooks",
        slug="runbooks",
    )
    agent = AgentDefinition.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        name="Knowledge agent",
        slug="knowledge-agent",
    )
    version = AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot={
            "model_config_id": str(chat_model.id),
            "system_prompt": "Answer operational questions.",
            "tool_allowlist": [],
            "knowledge_base_ids": [str(knowledge_base.id)],
            "max_iterations": 1,
            "timeout_seconds": 30,
            "max_tokens": 128,
        },
        created_by=scope["user"],
    )
    task = Task.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        task_type="agent.run",
        payload={},
        idempotency_key="knowledge-agent-run",
        status=Task.Status.RUNNING,
        lease_owner="worker-test",
        lease_token=uuid.uuid4(),
        available_at=timezone.now(),
    )
    AgentRun.objects.create(
        task=task,
        agent_version=version,
        input_text="What does the runbook say?",
    )

    class FakeGateway:
        def __init__(self):
            self.messages = None

        def complete(self, model_config, messages, **kwargs):
            self.messages = list(messages)
            return SimpleNamespace(
                content="The runbook says the cluster is healthy.",
                tool_calls=[],
                input_tokens=10,
                output_tokens=8,
                latency_ms=4,
                provider="fake",
                model="test-chat-model",
            )

    gateway = FakeGateway()
    with patch(
        "worker_app.tasks.retrieve_context",
        return_value=[
            {
                "text": "Check that all Kubernetes nodes are Ready.",
                "score": 0.91,
                "document_id": "document-1",
                "chunk_id": "document-1:0",
            }
        ],
    ) as retrieval:
        with patch.object(worker_tasks, "ModelGateway", return_value=gateway):
            result = worker_tasks.execute_agent_run(task)

    assert result["content"].startswith("The runbook says")
    assert "untrusted reference material" in gateway.messages[1]["content"]
    assert "document-1:0" in gateway.messages[1]["content"]
    assert "all Kubernetes nodes are Ready" in gateway.messages[1]["content"]
    retrieval.assert_called_once()
    assert retrieval.call_args.kwargs["metadata_filters"] == {
        "knowledge_base_id": str(knowledge_base.id)
    }


@pytest.mark.django_db
def test_knowledge_document_delete_cleans_vectors_and_source(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-delete",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="b" * 64, status=KnowledgeDocument.Status.READY, chunk_count=2,
    )
    with patch("core.knowledge_views.QdrantVectorStore") as store_class:
        response = scope["client"].delete(f"/api/v1/knowledge-documents/{document.id}/")
    assert response.status_code == 204
    store_class.return_value.delete_document.assert_called_once_with(
        tenant_id=str(scope["tenant"].id), project_id=str(scope["project"].id),
        document_id=str(document.id),
    )
    assert not KnowledgeDocument.objects.filter(pk=document.id).exists()


@pytest.mark.django_db
def test_knowledge_document_delete_retains_source_when_vector_cleanup_fails(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-delete-failure",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="c" * 64, status=KnowledgeDocument.Status.READY, chunk_count=2,
    )
    from core.vector_store import VectorStoreError

    with patch("core.knowledge_views.QdrantVectorStore") as store_class:
        store_class.return_value.delete_document.side_effect = VectorStoreError("unavailable")
        response = scope["client"].delete(f"/api/v1/knowledge-documents/{document.id}/")
    assert response.status_code == 503
    assert KnowledgeDocument.objects.filter(pk=document.id).exists()


@pytest.mark.django_db
def test_knowledge_document_delete_cancels_queued_ingestion(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-delete-queued",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="d" * 64,
    )
    task = Task.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        task_type="knowledge.ingest", payload={"knowledge_document_id": str(document.id)},
        idempotency_key="knowledge-delete-queued", available_at=timezone.now(),
    )
    with patch("core.knowledge_views.QdrantVectorStore"):
        response = scope["client"].delete(f"/api/v1/knowledge-documents/{document.id}/")
    task.refresh_from_db()
    assert response.status_code == 204
    assert task.status == Task.Status.CANCELLED
    assert not KnowledgeDocument.objects.filter(pk=document.id).exists()


@pytest.mark.django_db
def test_failed_knowledge_document_can_be_retried(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-retry",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="e" * 64, status=KnowledgeDocument.Status.FAILED,
        chunk_count=1, error={"code": "INGESTION_FAILED", "message": "Provider unavailable"},
    )
    with patch("core.knowledge_views.QdrantVectorStore") as store_class:
        response = scope["client"].post(
            f"/api/v1/knowledge-documents/{document.id}/retry/"
        )
    document.refresh_from_db()
    task = Task.objects.get(
        task_type="knowledge.ingest",
        payload__knowledge_document_id=str(document.id),
    )
    assert response.status_code == 202
    assert document.status == KnowledgeDocument.Status.PENDING
    assert document.chunk_count == 0
    assert document.error is None
    assert document.ingested_at is None
    assert task.status == Task.Status.QUEUED
    store_class.return_value.delete_document.assert_called_once_with(
        tenant_id=str(scope["tenant"].id), project_id=str(scope["project"].id),
        document_id=str(document.id),
    )


@pytest.mark.django_db
def test_ready_knowledge_document_cannot_be_retried(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-retry-ready",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="f" * 64, status=KnowledgeDocument.Status.READY,
        chunk_count=2,
    )
    with patch("core.knowledge_views.QdrantVectorStore") as store_class:
        response = scope["client"].post(
            f"/api/v1/knowledge-documents/{document.id}/retry/"
        )
    assert response.status_code == 409
    assert not Task.objects.filter(
        task_type="knowledge.ingest",
        payload__knowledge_document_id=str(document.id),
    ).exists()
    store_class.assert_not_called()


@pytest.mark.django_db
def test_retry_does_not_queue_when_vector_cleanup_fails(knowledge_scope):
    scope = knowledge_scope
    knowledge_base = KnowledgeBase.objects.create(
        tenant=scope["tenant"], project=scope["project"], created_by=scope["user"],
        embedding_model=scope["model"], name="Operations", slug="operations-retry-failure",
    )
    document = KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base, uploaded_by=scope["user"], title="Runbook",
        content="A runbook with cluster health checks.", metadata={"source": "test"},
        checksum="1" * 64, status=KnowledgeDocument.Status.FAILED,
    )
    from core.vector_store import VectorStoreError

    with patch("core.knowledge_views.QdrantVectorStore") as store_class:
        store_class.return_value.delete_document.side_effect = VectorStoreError("unavailable")
        response = scope["client"].post(
            f"/api/v1/knowledge-documents/{document.id}/retry/"
        )
    document.refresh_from_db()
    assert response.status_code == 503
    assert document.status == KnowledgeDocument.Status.FAILED
    assert not Task.objects.filter(
        task_type="knowledge.ingest",
        payload__knowledge_document_id=str(document.id),
    ).exists()
