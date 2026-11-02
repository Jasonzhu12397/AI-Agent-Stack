import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
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
from worker_app.tasks import process_once


@pytest.fixture
def agent_workspace(db):
    user = get_user_model().objects.create_user(
        username="agent-editor",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Agent tenant", slug="agent-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Agent project", slug="agent-project")
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Agent fake provider",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "agent runtime response"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="agent-fake-chat",
        display_name="Agent Fake Chat",
        capabilities=["chat"],
        max_output_tokens=1024,
    )
    return user, tenant, project, model


@pytest.mark.django_db
def test_agent_draft_publish_creates_immutable_version(agent_workspace):
    user, tenant, project, model = agent_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agents/"
    created = client.post(
        url,
        {
            "name": "Support agent",
            "slug": "support-agent",
            "description": "A test agent",
            "draft_config": {
                "model_config_id": str(model.id),
                "system_prompt": "You are a concise support assistant.",
                "tool_allowlist": [],
                "knowledge_base_ids": [],
                "max_iterations": 1,
                "timeout_seconds": 20,
                "max_tokens": 512,
            },
        },
        format="json",
    )
    assert created.status_code == 201
    agent_id = created.data["id"]

    published = client.post(f"/api/v1/agents/{agent_id}/publish/", {}, format="json")
    assert published.status_code == 201
    assert published.data["version"] == 1

    changed = client.patch(
        f"/api/v1/agents/{agent_id}/",
        {"draft_config": {"model_config_id": str(model.id), "system_prompt": "Updated draft"}},
        format="json",
    )
    assert changed.status_code == 200
    version = AgentVersion.objects.get(agent_id=agent_id, version=1)
    assert version.config_snapshot["system_prompt"] == "You are a concise support assistant."
    assert version.config_snapshot["model_config_id"] == str(model.id)


@pytest.mark.django_db
def test_agent_run_executes_through_persistent_worker(agent_workspace):
    user, tenant, project, model = agent_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    create_response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agents/",
        {
            "name": "Runnable agent",
            "slug": "runnable-agent",
            "draft_config": {
                "model_config_id": str(model.id),
                "system_prompt": "You are a test agent.",
                "max_iterations": 1,
                "timeout_seconds": 20,
                "max_tokens": 512,
                "tool_allowlist": [],
                "knowledge_base_ids": [],
            },
        },
        format="json",
    )
    agent_id = create_response.data["id"]
    published = client.post(f"/api/v1/agents/{agent_id}/publish/", {}, format="json")
    assert published.status_code == 201

    run_response = client.post(
        f"/api/v1/agents/{agent_id}/runs/",
        {"input": "Hello agent"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="agent-run-001",
    )
    assert run_response.status_code == 202
    run_id = run_response.data["id"]
    task_id = run_response.data["task_id"]
    assert run_response.data["task_status"] == Task.Status.QUEUED

    assert process_once("agent-worker-test") is True

    run = AgentRun.objects.select_related("task").get(pk=run_id)
    assert run.task.status == Task.Status.SUCCEEDED
    assert run.output_text == "agent runtime response"
    assert run.model_name == "agent-fake-chat"
    assert run.finished_at is not None
    assert Task.objects.get(pk=task_id).status == Task.Status.SUCCEEDED


@pytest.mark.django_db
def test_agent_cannot_publish_with_tools_or_knowledge_before_those_modules_exist(agent_workspace):
    user, tenant, project, model = agent_workspace
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Premature tools",
        slug="premature-tools",
        draft_config={
            "model_config_id": str(model.id),
            "system_prompt": "System prompt",
            "tool_allowlist": ["github.search"],
        },
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(f"/api/v1/agents/{agent.id}/publish/", {}, format="json")

    assert response.status_code == 400
    assert AgentVersion.objects.filter(agent=agent).count() == 0


@pytest.mark.django_db
def test_agent_run_requires_published_version_and_idempotency_key(agent_workspace):
    user, tenant, project, _ = agent_workspace
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Draft only",
        slug="draft-only",
    )
    client = APIClient()
    client.force_authenticate(user=user)

    not_published = client.post(
        f"/api/v1/agents/{agent.id}/runs/",
        {"input": "run me"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="agent-run-no-publish",
    )
    assert not_published.status_code == 409
    assert Task.objects.count() == 0
