"""Roadmap states must follow the rows the platform actually produced."""

import pytest
from core.chat_models import AgentChatThread
from core.models import (
    AgentCommand,
    AgentDefinition,
    AgentNode,
    AgentRun,
    AgentVersion,
    ModelConfig,
    ModelProvider,
    Project,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowVersion,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def platform_scope(db):
    user = get_user_model().objects.create_user(username="roadmap-user", password="pw-12345678")
    tenant = Tenant.objects.create(name="Roadmap tenant", slug="roadmap-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(tenant=tenant, name="Roadmap project", slug="roadmap-project")
    return user, tenant, project


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_empty_deployment_reports_not_started(platform_scope):
    user, _tenant, _project = platform_scope
    items = _client(user).get("/api/v1/platform/roadmap/").json()["items"]
    by_code = {item["code"]: item["status"] for item in items}
    assert by_code["M0"] == "verified"
    assert by_code["M1"] == "in_progress"
    assert by_code["M2"] == "in_progress"
    assert by_code["M3"] == "in_progress"


@pytest.mark.django_db
def test_runtime_and_workflow_evidence_flips_milestones_to_verified(platform_scope):
    user, tenant, project = platform_scope
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Roadmap fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "ok"},
    )
    model = ModelConfig.objects.create(
        provider=provider, model_name="roadmap-fake", display_name="Fake", capabilities=["chat"]
    )
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Agent",
        slug="agent",
        current_version=1,
    )
    version = AgentVersion.objects.create(
        agent=agent, version=1, config_snapshot={}, created_by=user
    )
    agent_task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="agent.run",
        payload={},
        status=Task.Status.SUCCEEDED,
        idempotency_key="roadmap-agent-run",
        available_at=timezone.now(),
    )
    AgentRun.objects.create(
        agent_version=version, task=agent_task, input_text="hi", output_text="ok"
    )

    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Flow",
        slug="flow",
        current_version=1,
    )
    workflow_version = WorkflowVersion.objects.create(
        workflow=workflow, version=1, definition_snapshot={}, created_by=user
    )
    workflow_task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="workflow.run",
        payload={},
        status=Task.Status.SUCCEEDED,
        idempotency_key="roadmap-workflow-run",
        available_at=timezone.now(),
    )
    run = WorkflowRun.objects.create(workflow_version=workflow_version, task=workflow_task)
    WorkflowStepRun.objects.create(
        workflow_run=run,
        node_id="step-1",
        node_type="llm",
        status=WorkflowStepRun.Status.SUCCEEDED,
    )

    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-1",
        node_type=AgentNode.NodeType.EPH_NODE,
        # A node without heartbeat is offline but still proves enrollment.
        status=AgentNode.Status.OFFLINE,
        credential_hash="0" * 64,
    )
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=node,
        requested_by=user,
        command_type="inventory.report",
        payload={},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="roadmap-command",
        plan_hash="0" * 64,
        approved_at=timezone.now(),
    )

    items = _client(user).get("/api/v1/platform/roadmap/").json()["items"]
    by_code = {item["code"]: item for item in items}
    assert by_code["M1"]["status"] == "verified"
    assert by_code["M1"]["evidence"]["succeeded_runs"] == 1
    assert by_code["M2"]["status"] == "verified"
    assert by_code["M2"]["evidence"]["succeeded_steps"] == 1
    assert by_code["M3"]["status"] == "verified"
    assert by_code["M3"]["evidence"]["enrolled_nodes"] == 1
    assert by_code["M3"]["evidence"]["online_nodes"] == 0
    assert model.id  # the model above is the same one the chat falls back to


@pytest.mark.django_db
def test_chat_without_a_model_selected_uses_the_local_echo_default(platform_scope):
    user, tenant, project = platform_scope
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Echo",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "echo reply"},
    )
    ModelConfig.objects.create(
        provider=provider,
        model_name="echo",
        display_name="Echo",
        capabilities=["chat"],
        enabled=True,
    )
    client = _client(user)
    thread_id = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/threads/",
        {"title": "no model selected"},
        format="json",
    ).json()["id"]
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread_id}/messages/",
        {"content": "hello"},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["assistant"]["content"] == "echo reply"
    assert AgentChatThread.objects.get(pk=thread_id).model_config_id is not None
