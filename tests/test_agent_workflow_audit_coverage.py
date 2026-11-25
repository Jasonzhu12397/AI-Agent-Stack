"""Agent and workflow write endpoints must record audit events.

These builders change what the platform will execute on behalf of a tenant, so
the audit trail has to name the actor, the resource and the version that was
materialised. Draft-only edits (``agent.updated`` / ``workflow.updated``) are
audited too: they change future publish output even before a version exists.
"""

import pytest
from core.models import (
    AgentDefinition,
    AuditEvent,
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


def _workflow_definition(model_id):
    return {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "answer",
                "type": "llm",
                "config": {
                    "model_config_id": str(model_id),
                    "prompt_template": "Answer this: {{input}}",
                    "max_tokens": 256,
                    "timeout_seconds": 20,
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "answer"}},
        ],
        "edges": [
            {"source": "start", "target": "answer"},
            {"source": "answer", "target": "end"},
        ],
    }


@pytest.fixture
def builder_scope(db):
    user = get_user_model().objects.create_user(username="builder-editor")
    tenant = Tenant.objects.create(name="Builder tenant", slug="builder-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.MEMBER)
    project = Project.objects.create(tenant=tenant, name="Builder project", slug="builder-project")
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.EDITOR,
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Builder fake provider",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "builder response"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="builder-fake-chat",
        display_name="Builder fake chat",
        capabilities=["chat"],
        max_output_tokens=1024,
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


def _events(tenant, event_type):
    return AuditEvent.objects.filter(tenant=tenant, event_type=event_type)


def _draft_config(model_id):
    return {
        "model_config_id": str(model_id),
        "system_prompt": "You are a concise assistant.",
        "knowledge_base_ids": [],
        "max_iterations": 1,
        "timeout_seconds": 20,
        "max_tokens": 512,
    }


@pytest.mark.django_db
def test_agent_creation_and_update_are_audited(builder_scope):
    client = builder_scope["client"]
    url = (
        f"/api/v1/tenants/{builder_scope['tenant'].id}"
        f"/projects/{builder_scope['project'].id}/agents/"
    )
    created = client.post(
        url,
        {
            "name": "Audited agent",
            "slug": "audited-agent",
            "draft_config": _draft_config(builder_scope["model"].id),
        },
        format="json",
    )
    assert created.status_code == 201
    agent_id = created.data["id"]

    create_event = _events(builder_scope["tenant"], "agent.created").get()
    assert create_event.resource_type == "agent_definition"
    assert create_event.resource_id == agent_id
    assert create_event.metadata["slug"] == "audited-agent"

    updated = client.patch(
        f"/api/v1/agents/{agent_id}/",
        {"description": "edited description"},
        format="json",
    )
    assert updated.status_code == 200

    update_event = _events(builder_scope["tenant"], "agent.updated").get()
    assert update_event.resource_id == agent_id
    assert update_event.metadata["fields"] == ["description"]
    assert update_event.metadata["previous_current_version"] == 0


@pytest.mark.django_db
def test_agent_publish_is_audited_with_the_materialised_version(builder_scope):
    client = builder_scope["client"]
    created = client.post(
        (
            f"/api/v1/tenants/{builder_scope['tenant'].id}"
            f"/projects/{builder_scope['project'].id}/agents/"
        ),
        {
            "name": "Publishable agent",
            "slug": "publishable-agent",
            "draft_config": _draft_config(builder_scope["model"].id),
        },
        format="json",
    )
    assert created.status_code == 201
    agent_id = created.data["id"]

    published = client.post(f"/api/v1/agents/{agent_id}/publish/", {}, format="json")
    assert published.status_code == 201

    event = _events(builder_scope["tenant"], "agent.published").get()
    assert event.resource_type == "agent_version"
    assert event.resource_id == published.data["id"]
    assert event.metadata["agent_id"] == agent_id
    assert event.metadata["version"] == 1
    assert event.metadata["model_config_id"] == str(builder_scope["model"].id)
    assert event.metadata["tool_count"] == 0
    assert event.metadata["knowledge_base_count"] == 0


@pytest.mark.django_db
def test_agent_run_creation_is_audited(builder_scope):
    user = builder_scope["user"]
    agent = AgentDefinition.objects.create(
        tenant=builder_scope["tenant"],
        project=builder_scope["project"],
        created_by=user,
        name="Runnable agent",
        slug="runnable-agent",
        draft_config=_draft_config(builder_scope["model"].id),
        current_version=1,
    )
    from core.models import AgentVersion

    AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot=_draft_config(builder_scope["model"].id),
        created_by=user,
    )

    response = builder_scope["client"].post(
        f"/api/v1/agents/{agent.id}/runs/",
        {"input": "hello"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="audit-agent-run-1",
    )
    assert response.status_code == 202

    event = _events(builder_scope["tenant"], "agent.run.created").get()
    assert event.resource_type == "agent_run"
    assert event.resource_id == response.data["id"]
    assert event.metadata["agent_id"] == str(agent.id)
    assert event.metadata["deduplicated"] is False


@pytest.mark.django_db
def test_workflow_creation_update_publish_and_run_are_audited(builder_scope):
    client = builder_scope["client"]
    tenant = builder_scope["tenant"]
    url = (
        f"/api/v1/tenants/{tenant.id}"
        f"/projects/{builder_scope['project'].id}/workflows/"
    )
    created = client.post(
        url,
        {
            "name": "Audited workflow",
            "slug": "audited-workflow",
            "draft_definition": _workflow_definition(builder_scope["model"].id),
        },
        format="json",
    )
    assert created.status_code == 201
    workflow_id = created.data["id"]

    assert _events(tenant, "workflow.created").get().resource_id == workflow_id

    updated = client.patch(
        f"/api/v1/workflows/{workflow_id}/",
        {"description": "edited workflow"},
        format="json",
    )
    assert updated.status_code == 200
    update_event = _events(tenant, "workflow.updated").get()
    assert update_event.metadata["fields"] == ["description"]

    published = client.post(f"/api/v1/workflows/{workflow_id}/publish/", {}, format="json")
    assert published.status_code == 201
    publish_event = _events(tenant, "workflow.published").get()
    assert publish_event.resource_type == "workflow_version"
    assert publish_event.metadata["workflow_id"] == workflow_id
    assert publish_event.metadata["version"] == 1
    assert publish_event.metadata["step_count"] == 3

    run = client.post(
        f"/api/v1/workflows/{workflow_id}/runs/",
        {"input": {"question": "audit me"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="audit-workflow-run-1",
    )
    assert run.status_code == 202
    run_event = _events(tenant, "workflow.run.created").get()
    assert run_event.resource_id == run.data["id"]
    assert run_event.metadata["workflow_id"] == workflow_id


@pytest.mark.django_db
def test_workflow_definition_is_created_from_the_api_not_the_database(builder_scope):
    workflow = WorkflowDefinition.objects.create(
        tenant=builder_scope["tenant"],
        project=builder_scope["project"],
        created_by=builder_scope["user"],
        name="Direct workflow",
        slug="direct-workflow",
        draft_definition=_workflow_definition(builder_scope["model"].id),
    )
    assert workflow.current_version == 0
    assert not AuditEvent.objects.filter(
        tenant=builder_scope["tenant"],
        event_type="workflow.created",
    ).exists()
