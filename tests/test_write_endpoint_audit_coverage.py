"""Every write endpoint must leave a tamper-evident audit event behind.

The audit trail is the only record of who changed model providers, model
configs, knowledge bases and knowledge documents. These tests pin the coverage
by exercising the HTTP surface and asserting the persisted event type, resource
type and sanitized metadata.
"""

import pytest
from core.models import (
    AuditEvent,
    KnowledgeBase,
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def audit_scope(db):
    user = get_user_model().objects.create_user(username="audit-actor")
    tenant = Tenant.objects.create(name="Audit scope", slug="audit-scope")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Audit project", slug="audit-project")
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Audit embedding provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="audit-embedding-model",
        display_name="Audit embedding model",
        capabilities=["embeddings"],
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {
        "user": user,
        "tenant": tenant,
        "project": project,
        "provider": provider,
        "model": model,
        "client": client,
    }


def _events(tenant, event_type):
    return AuditEvent.objects.filter(tenant=tenant, event_type=event_type)


@pytest.mark.django_db
def test_model_provider_creation_is_audited(audit_scope):
    response = audit_scope["client"].post(
        f"/api/v1/tenants/{audit_scope['tenant'].id}/model-providers/",
        {
            "name": "Second provider",
            "provider_type": ModelProvider.ProviderType.FAKE,
        },
        format="json",
    )
    assert response.status_code == 201

    event = _events(audit_scope["tenant"], "model.provider.created").get()
    assert event.resource_type == "model_provider"
    assert event.resource_id == response.data["id"]
    assert event.metadata["name"] == "Second provider"
    assert event.outcome == AuditEvent.Outcome.SUCCESS


@pytest.mark.django_db
def test_model_config_creation_is_audited(audit_scope):
    provider_id = audit_scope["provider"].id
    response = audit_scope["client"].post(
        f"/api/v1/tenants/{audit_scope['tenant'].id}/model-providers/{provider_id}/models/",
        {
            "model_name": "audit-chat-model",
            "display_name": "Audit chat model",
            "capabilities": ["chat"],
        },
        format="json",
    )
    assert response.status_code == 201

    event = _events(audit_scope["tenant"], "model.config.created").get()
    assert event.resource_type == "model_config"
    assert event.resource_id == response.data["id"]
    assert event.metadata["model_name"] == "audit-chat-model"
    assert event.metadata["provider_id"] == str(provider_id)
    assert event.metadata["version"] == 1


@pytest.mark.django_db
def test_knowledge_base_creation_is_audited(audit_scope):
    response = audit_scope["client"].post(
        (
            f"/api/v1/tenants/{audit_scope['tenant'].id}"
            f"/projects/{audit_scope['project'].id}/knowledge-bases/"
        ),
        {
            "name": "Audited knowledge",
            "slug": "audited-knowledge",
            "embedding_model_id": str(audit_scope["model"].id),
        },
        format="json",
    )
    assert response.status_code == 201

    event = _events(audit_scope["tenant"], "knowledge.base.created").get()
    assert event.resource_type == "knowledge_base"
    assert event.resource_id == response.data["id"]
    assert event.metadata["slug"] == "audited-knowledge"


@pytest.mark.django_db
def test_knowledge_document_creation_is_audited(audit_scope):
    knowledge_base = KnowledgeBase.objects.create(
        tenant=audit_scope["tenant"],
        project=audit_scope["project"],
        created_by=audit_scope["user"],
        embedding_model=audit_scope["model"],
        name="Document knowledge",
        slug="document-knowledge",
    )
    response = audit_scope["client"].post(
        f"/api/v1/knowledge-bases/{knowledge_base.id}/documents/",
        {
            "title": "Audited runbook",
            "content": "Check API health and node readiness.",
            "metadata": {"source": "runbook"},
        },
        format="json",
    )
    assert response.status_code == 202

    event = _events(audit_scope["tenant"], "knowledge.document.created").get()
    assert event.resource_type == "knowledge_document"
    assert event.resource_id == response.data["id"]
    assert event.metadata["title"] == "Audited runbook"
    assert event.metadata["knowledge_base_id"] == str(knowledge_base.id)


@pytest.mark.django_db
def test_model_test_call_records_a_failure_event(audit_scope):
    from core.model_gateway import ModelGatewayError

    class _FailingGateway:
        def __init__(self, guard=None):
            pass

        def complete(self, model_config, messages, **kwargs):
            raise ModelGatewayError("MODEL_GATEWAY_UNAVAILABLE", "gateway is offline")

    from core import model_views

    original = model_views.ModelGateway
    model_views.ModelGateway = _FailingGateway
    try:
        response = audit_scope["client"].post(
            (
                f"/api/v1/tenants/{audit_scope['tenant'].id}"
                f"/model-configs/{audit_scope['model'].id}/test/"
            ),
            {"messages": [{"role": "user", "content": "ping"}]},
            format="json",
        )
    finally:
        model_views.ModelGateway = original

    assert response.status_code == 502
    event = _events(audit_scope["tenant"], "model.config.tested").get()
    assert event.outcome == AuditEvent.Outcome.FAILURE
    assert event.metadata["error_code"] == "MODEL_GATEWAY_UNAVAILABLE"
