import hashlib

import pytest
from core.models import AgentCommand, AgentNode, Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def node_workspace(db):
    user = get_user_model().objects.create_user(
        username="node-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Node tenant", slug="node-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Node project", slug="node-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


@pytest.mark.django_db
def test_eph_node_enrollment_returns_one_time_token_and_stores_only_hash(node_workspace):
    user, tenant, project, client = node_workspace
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-01", "node_type": "eph_node"},
        format="json",
    )

    assert response.status_code == 201
    node = AgentNode.objects.get(pk=response.data["id"])
    token = response.data["enrollment_token"]
    assert node.created_by == user
    assert node.node_type == AgentNode.NodeType.EPH_NODE
    assert node.credential_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in str(node.__dict__)
    assert "credential_hash" not in response.data


@pytest.mark.django_db
def test_node_agent_enrollment_is_tenant_admin_only(node_workspace):
    user, tenant, project, _ = node_workspace
    member = get_user_model().objects.create_user(username="node-member", password="test-password")
    TenantMembership.objects.create(tenant=tenant, user=member, role=TenantMembership.Role.MEMBER)
    ProjectMembership.objects.create(project=project, user=member, role=ProjectMembership.Role.EDITOR)
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "forbidden", "node_type": "eph_node"},
        format="json",
    )

    assert response.status_code == 403
    assert AgentNode.objects.count() == 0


@pytest.mark.django_db
def test_eph_provision_command_waits_for_hash_bound_approval_and_is_leased(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-approval", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    payload = {
        "cluster_spec": {
            "cluster_name": "edge-lab",
            "kubernetes_version": "v1.32.2",
            "control_plane_replicas": 3,
            "worker_replicas": 4,
        }
    }
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "cluster.provision", "payload": payload},
        format="json",
        HTTP_IDEMPOTENCY_KEY="eph-provision-001",
    )
    assert created.status_code == 202
    assert created.data["status"] == AgentCommand.Status.WAITING_APPROVAL

    unauthorized_poll = APIClient().post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION="Bearer wrong-token",
    )
    assert unauthorized_poll.status_code == 401

    approved = client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    assert approved.status_code == 200
    assert approved.data["status"] == AgentCommand.Status.QUEUED

    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert polled.status_code == 200
    command = polled.data["command"]
    assert command["id"] == created.data["id"]
    assert command["command_type"] == "cluster.provision"
    assert command["lease_token"]


@pytest.mark.django_db
def test_target_cluster_agent_cannot_receive_eph_only_commands(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "target-01", "node_type": "target_cluster"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.provision", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="target-cannot-provision",
    )

    assert response.status_code == 400
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
def test_agent_command_payload_rejects_shell_and_raw_manifest(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-secure", "node_type": "eph_node"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.plan", "payload": {"shell": "rm -rf /"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="reject-shell-command",
    )

    assert response.status_code == 400
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
def test_agent_result_requires_current_lease_token(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-result", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    command_response = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "node.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="node-health-result",
    )
    agent_client = APIClient()
    poll = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    command = poll.data["command"]

    stale = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {"node_id": node_id, "outcome": "succeeded", "result": {"ok": True}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE="00000000-0000-0000-0000-000000000000",
    )
    accepted = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {"node_id": node_id, "outcome": "succeeded", "result": {"ok": True}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

    assert command_response.status_code == 202
    assert stale.status_code == 404
    assert accepted.status_code == 200
    assert accepted.data["status"] == AgentCommand.Status.SUCCEEDED
    assert AgentCommand.objects.get(pk=command["id"]).finished_at is not None


@pytest.mark.django_db
def test_heartbeat_marks_node_online_and_revocation_blocks_future_calls(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-revoke", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    agent_client = APIClient()
    heartbeat = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": node_id, "agent_version": "0.1.0"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert heartbeat.status_code == 200
    assert AgentNode.objects.get(pk=node_id).status == AgentNode.Status.ONLINE

    revoked = client.post(f"/api/v1/agent-nodes/{node_id}/revoke/", {}, format="json")
    after_revoke = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert revoked.status_code == 200
    assert revoked.data["status"] == AgentNode.Status.REVOKED
    assert after_revoke.status_code == 401
