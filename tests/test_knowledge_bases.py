import pytest
from core.models import (
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
