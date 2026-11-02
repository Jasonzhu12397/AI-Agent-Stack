from unittest.mock import patch

import pytest
from core.models import (
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
from django.utils import timezone
from worker_app.tasks import execute_knowledge_ingest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


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
