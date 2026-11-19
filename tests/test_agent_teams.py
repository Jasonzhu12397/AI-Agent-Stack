from uuid import uuid4

import pytest
from core.models import (
    AgentDefinition,
    AgentTeam,
    AgentTeamRun,
    AgentTeamVersion,
    AgentVersion,
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowStepRun,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from worker_app.tasks import process_once


@pytest.fixture
def team_workspace(db):
    user = get_user_model().objects.create_user(
        username="team-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Team tenant", slug="team-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(
        tenant=tenant,
        name="Team project",
        slug="team-project",
    )
    ProjectMembership.objects.create(project=project, user=user, role="admin")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Team fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "team member response"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="team-fake",
        display_name="Team Fake",
        capabilities=["chat"],
        max_output_tokens=512,
    )
    return user, tenant, project, model


def make_agent(user, tenant, project, model, name, tool_allowlist=None):
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name=name,
        slug=name.lower().replace(" ", "-"),
        draft_config={
            "model_config_id": str(model.id),
            "system_prompt": f"You are {name}.",
        },
        current_version=1,
    )
    AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot={
            "model_config_id": str(model.id),
            "system_prompt": f"You are {name}.",
            "tool_allowlist": tool_allowlist or [],
            "knowledge_base_ids": [],
            "max_iterations": 2,
            "timeout_seconds": 30,
            "max_tokens": 128,
        },
        created_by=user,
    )
    return agent


def team_payload(members, coordinator=None):
    payload = {
        "strategy": "coordinator" if coordinator else "sequential",
        "members": [
            {
                "agent_id": str(member.id),
                "role": "specialist",
                "instructions": "Handle your part of the task.",
            }
            for member in members
        ],
    }
    if coordinator is not None:
        payload["coordinator"] = {
            "agent_id": str(coordinator.id),
            "instructions": "Combine teammate outputs into one final answer.",
        }
    return payload


@pytest.mark.django_db
def test_team_publish_builds_multi_agent_workflow(team_workspace):
    user, tenant, project, model = team_workspace
    first = make_agent(user, tenant, project, model, "Research Agent")
    second = make_agent(user, tenant, project, model, "Writer Agent")
    coordinator = make_agent(user, tenant, project, model, "Editor Agent")
    client = APIClient()
    client.force_authenticate(user=user)

    created = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-teams/",
        {
            "name": "Research and write",
            "slug": "research-and-write",
            "description": "Two specialists plus an editor",
            "draft_config": team_payload([first, second], coordinator),
        },
        format="json",
    )
    assert created.status_code == 201
    team_id = created.data["id"]

    published = client.post(
        f"/api/v1/agent-teams/{team_id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201

    team = AgentTeam.objects.get(pk=team_id)
    version = AgentTeamVersion.objects.get(pk=published.data["id"])
    assert team.current_version == 1
    assert version.workflow_version.definition_snapshot["nodes"][-2]["type"] == "agent"
    node_types = [
        node["type"] for node in version.workflow_version.definition_snapshot["nodes"]
    ]
    assert node_types == ["start", "agent", "agent", "agent", "end"]
    assert version.definition_snapshot["strategy"] == "coordinator"
    assert len(version.definition_snapshot["members"]) == 2


@pytest.mark.django_db
def test_team_run_executes_every_member_and_coordinator(team_workspace):
    user, tenant, project, model = team_workspace
    first = make_agent(user, tenant, project, model, "Research Agent")
    second = make_agent(user, tenant, project, model, "Writer Agent")
    coordinator = make_agent(user, tenant, project, model, "Editor Agent")
    client = APIClient()
    client.force_authenticate(user=user)

    created = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-teams/",
        {
            "name": "Research and write",
            "slug": "research-and-write",
            "draft_config": team_payload([first, second], coordinator),
        },
        format="json",
    )
    team_id = created.data["id"]
    published = client.post(
        f"/api/v1/agent-teams/{team_id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201

    run_response = client.post(
        f"/api/v1/agent-teams/{team_id}/runs/",
        {"input": "Write a short note about offline Kubernetes installs."},
        format="json",
        HTTP_IDEMPOTENCY_KEY="team-run-001",
    )
    assert run_response.status_code == 202
    run_id = run_response.data["id"]
    assert run_response.data["task_status"] == Task.Status.QUEUED

    assert process_once("team-test-worker") is True
    run = AgentTeamRun.objects.select_related("workflow_run__task").get(pk=run_id)
    assert run.workflow_run.task.status == Task.Status.SUCCEEDED
    assert run.output_text == "team member response"
    assert run.finished_at is not None
    succeeded = WorkflowStepRun.objects.filter(
        workflow_run=run.workflow_run,
        status=WorkflowStepRun.Status.SUCCEEDED,
    )
    assert succeeded.count() == 5
    assert succeeded.filter(node_type="agent").count() == 3

    repeated = client.post(
        f"/api/v1/agent-teams/{team_id}/runs/",
        {"input": "Write a short note about offline Kubernetes installs."},
        format="json",
        HTTP_IDEMPOTENCY_KEY="team-run-001",
    )
    assert repeated.status_code == 200
    assert repeated.data["id"] == run_id


@pytest.mark.django_db
def test_team_publish_rejects_agent_with_plugin_tools(team_workspace):
    user, tenant, project, model = team_workspace
    tool_agent = make_agent(
        user,
        tenant,
        project,
        model,
        "Tool Agent",
        tool_allowlist=[str(uuid4())],
    )
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-teams/",
        {
            "name": "Tool team",
            "slug": "tool-team",
            "draft_config": team_payload([tool_agent]),
        },
        format="json",
    )
    assert created.status_code == 201

    published = client.post(
        f"/api/v1/agent-teams/{created.data['id']}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 400
    assert published.data["code"] == "TEAM_AGENT_TOOLS_UNSUPPORTED"
    assert AgentTeamVersion.objects.count() == 0


@pytest.mark.django_db
def test_team_run_requires_published_version(team_workspace):
    user, tenant, project, model = team_workspace
    agent = make_agent(user, tenant, project, model, "Research Agent")
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-teams/",
        {
            "name": "Draft team",
            "slug": "draft-team",
            "draft_config": team_payload([agent]),
        },
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-teams/{created.data['id']}/runs/",
        {"input": "Hello"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="team-draft-run",
    )
    assert response.status_code == 409
    assert Task.objects.count() == 0


@pytest.mark.django_db
def test_team_detail_is_scoped_to_project_membership(team_workspace):
    user, tenant, project, model = team_workspace
    agent = make_agent(user, tenant, project, model, "Private Agent")
    outsider = get_user_model().objects.create_user(
        username="team-outsider",
        password="test-password",
    )
    TenantMembership.objects.create(tenant=tenant, user=outsider, role="member")
    team = AgentTeam.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Private team",
        slug="private-team",
        draft_config=team_payload([agent]),
    )
    client = APIClient()
    client.force_authenticate(user=outsider)
    response = client.get(f"/api/v1/agent-teams/{team.id}/")
    assert response.status_code == 404
