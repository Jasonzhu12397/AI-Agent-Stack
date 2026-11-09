# ruff: noqa: I001
import datetime
import hashlib

import pytest
from core.models import (
    AgentCommand,
    AgentMachine,
    AgentNode,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
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
    ProjectMembership.objects.create(
        project=project,
        user=member,
        role=ProjectMembership.Role.EDITOR,
    )
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

    self_approval = client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    approver = get_user_model().objects.create_user(
        username="node-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    approver_client = APIClient()
    approver_client.force_authenticate(user=approver)
    approved = approver_client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    assert self_approval.status_code == 403
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
def test_agent_command_payload_enforces_utf8_byte_limit(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-unicode-limit", "node_type": "eph_node"},
        format="json",
    )
    payload = {"description": "汉" * 22000}

    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.plan", "payload": payload},
        format="json",
        HTTP_IDEMPOTENCY_KEY="unicode-payload-byte-limit",
    )

    assert len(str(payload)) < 65536
    assert response.status_code == 400
    assert "64 KiB" in response.data["detail"]
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
    renewed = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/lease/renew/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

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
    assert renewed.status_code == 200
    assert renewed.data["lease_expires_at"] is not None
    assert stale.status_code == 404
    assert accepted.status_code == 200
    assert accepted.data["status"] == AgentCommand.Status.SUCCEEDED
    assert AgentCommand.objects.get(pk=command["id"]).finished_at is not None


@pytest.mark.django_db
def test_agent_command_result_enforces_utf8_byte_limit(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-unicode-result", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "node.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="unicode-result-byte-limit",
    )
    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    command = polled.data["command"]
    result = {"description": "汉" * 22000}

    response = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {"node_id": node_id, "outcome": "succeeded", "result": result},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

    assert created.status_code == 202
    assert len(str(result)) < 65536
    assert response.status_code == 400
    assert "64 KiB" in response.data["detail"]
    stored = AgentCommand.objects.get(pk=command["id"])
    assert stored.status == AgentCommand.Status.RUNNING
    assert stored.lease_token is not None


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


@pytest.mark.django_db
def test_revoking_node_cancels_running_command_and_clears_lease(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-running-revoke", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "node.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="revoke-running-command",
    )
    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    command_id = polled.data["command"]["id"]

    revoked = client.post(f"/api/v1/agent-nodes/{node_id}/revoke/", {}, format="json")

    command = AgentCommand.objects.get(pk=command_id)
    assert revoked.status_code == 200
    assert command.status == AgentCommand.Status.CANCELLED
    assert command.lease_token is None
    assert command.lease_expires_at is None


@pytest.mark.django_db
def test_agent_runtime_rejects_malformed_node_id(node_workspace):
    user, tenant, project, client = node_workspace
    response = APIClient().post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": "not-a-uuid"},
        format="json",
        HTTP_AUTHORIZATION="Bearer arbitrary-token",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_provisioning_command_approval_expires_before_agent_dispatch(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-expiring-approval", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {
            "command_type": "cluster.provision",
            "payload": {"cluster_spec": {"cluster_name": "edge-lab"}},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="approval-expiry-test",
    )
    approver = get_user_model().objects.create_user(
        username="node-expiry-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    approver_client = APIClient()
    approver_client.force_authenticate(user=approver)
    approved = approver_client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    command = AgentCommand.objects.get(pk=created.data["id"])
    command.approved_at = command.approved_at - datetime.timedelta(minutes=16)
    command.save(update_fields=["approved_at"])

    poll = APIClient().post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    command.refresh_from_db()
    assert approved.status_code == 200
    assert poll.status_code == 200
    assert poll.data["command"] is None
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == "APPROVAL_EXPIRED"



@pytest.mark.django_db
def test_agent_poll_rejects_command_not_allowed_for_node_role(node_workspace):
    user, tenant, project, client = node_workspace
    token = "wrong-role-poll-token"
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-wrong-role",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=node,
        requested_by=user,
        command_type="cluster.postflight",
        payload={},
        status=AgentCommand.Status.QUEUED,
        idempotency_key="wrong-role-command",
        plan_hash="d" * 64,
    )

    response = APIClient().post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    command.refresh_from_db()
    assert response.status_code == 200
    assert response.data["command"] is None
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == "COMMAND_NOT_ALLOWED_FOR_AGENT_ROLE"



@pytest.mark.django_db
def test_target_agent_control_plane_verify_command_is_allowed(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "target-control-plane-verifier", "node_type": "target_cluster"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.control-plane.verify",
            "payload": {"expected_control_plane_replicas": 1},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="target-control-plane-verify",
    )
    assert response.status_code == 202


@pytest.mark.django_db
def test_target_agent_capi_pivot_preflight_command_is_allowed(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "target-capi-pivot-preflight", "node_type": "target_cluster"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.capi.pivot.target.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="target-capi-pivot-preflight",
    )
    assert response.status_code == 202


@pytest.mark.django_db
def test_eph_agent_capi_pivot_source_preflight_command_is_allowed(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-capi-pivot-source-preflight", "node_type": "eph_node"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.capi.pivot.source.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="eph-capi-pivot-source-preflight",
    )
    assert response.status_code == 202


@pytest.mark.django_db
def test_project_agent_command_history_is_scoped_and_paginated(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "fleet-history-eph", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "host.preflight", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="fleet-history-command-1",
    )
    assert created.status_code == 202

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-commands/?limit=1&offset=0",
    )

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["limit"] == 1
    assert response.data["results"][0]["id"] == created.data["id"]
    assert response.data["results"][0]["node_id"] == node_id

    filtered = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-commands/?status=queued&node_id={node_id}&command_type=host.preflight",
    )
    assert filtered.status_code == 200
    assert filtered.data["count"] == 1

    invalid_node = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-commands/?node_id=not-a-uuid",
    )
    assert invalid_node.status_code == 400

    invalid_limit = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-commands/?limit=201",
    )
    assert invalid_limit.status_code == 400



@pytest.mark.django_db
def test_capi_pivot_execution_requires_separate_admin_approval(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-pivot", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {
            "command_type": "cluster.capi.pivot.execute",
            "payload": {
                "cluster_name": "edge-lab",
                "namespace": "metal3",
            },
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="capi-pivot-approval-001",
    )

    assert created.status_code == 202
    assert created.data["status"] == AgentCommand.Status.WAITING_APPROVAL

    self_approval = client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    approver = get_user_model().objects.create_user(
        username="pivot-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    approver_client = APIClient()
    approver_client.force_authenticate(user=approver)
    approved = approver_client.post(
        f"/api/v1/agent-commands/{created.data['id']}/approve/",
        {"plan_hash": created.data["plan_hash"]},
        format="json",
    )
    assert self_approval.status_code == 403
    assert approved.status_code == 200
    assert approved.data["status"] == AgentCommand.Status.QUEUED

    polled = APIClient().post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert polled.status_code == 200
    assert polled.data["command"]["id"] == created.data["id"]
    assert polled.data["command"]["command_type"] == "cluster.capi.pivot.execute"
    assert polled.data["command"]["lease_token"]
    assert AgentCommand.objects.get(pk=created.data["id"]).status == (
        AgentCommand.Status.RUNNING
    )


@pytest.mark.django_db
@pytest.mark.parametrize("node_type", ["eph_node", "target_cluster"])
def test_network_inspection_command_can_be_queued_for_both_agent_roles(
    node_workspace,
    node_type,
):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": f"network-inspector-{node_type}", "node_type": node_type},
        format="json",
    )

    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.network.inspect", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"network-inspect-{node_type}",
    )

    assert response.status_code == 202
    assert response.data["command_type"] == "cluster.network.inspect"
    assert response.data["status"] == AgentCommand.Status.QUEUED
    assert response.data["approved_at"] is None


@pytest.mark.django_db
def test_agent_credential_rotation_invalidates_old_token_and_requires_new_token(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "rotate-agent", "node_type": "target_cluster"},
        format="json",
    )
    node_id = enrolled.data["id"]
    old_token = enrolled.data["enrollment_token"]

    rotated = client.post(
        f"/api/v1/agent-nodes/{node_id}/credentials/rotate/",
        {},
        format="json",
    )

    assert rotated.status_code == 200
    new_token = rotated.data["enrollment_token"]
    assert new_token != old_token
    node = AgentNode.objects.get(pk=node_id)
    assert node.status == AgentNode.Status.OFFLINE
    assert node.credential_hash == hashlib.sha256(new_token.encode("utf-8")).hexdigest()
    assert "credential_hash" not in rotated.data

    runtime = APIClient()
    old_heartbeat = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {old_token}",
    )
    new_heartbeat = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {new_token}",
    )

    assert old_heartbeat.status_code == 401
    assert new_heartbeat.status_code == 200
    assert new_heartbeat.data["status"] == AgentNode.Status.ONLINE


@pytest.mark.django_db
def test_agent_credential_rotation_is_blocked_while_command_is_running(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "busy-agent", "node_type": "target_cluster"},
        format="json",
    )
    node_id = enrolled.data["id"]
    command = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "cluster.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="busy-agent-command",
    )
    assert command.status_code == 202
    AgentCommand.objects.filter(pk=command.data["id"]).update(
        status=AgentCommand.Status.RUNNING,
    )
    node = AgentNode.objects.get(pk=node_id)
    original_hash = node.credential_hash

    rotated = client.post(
        f"/api/v1/agent-nodes/{node_id}/credentials/rotate/",
        {},
        format="json",
    )

    assert rotated.status_code == 409
    node.refresh_from_db()
    assert node.credential_hash == original_hash

@pytest.mark.django_db
def test_worker_provisioning_command_cannot_be_created_outside_deployment_workflow(
    node_workspace,
):
    user, tenant, project, client = node_workspace
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-worker-provisioning",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash=hashlib.sha256(b"target-token").hexdigest(),
    )

    response = client.post(
        f"/api/v1/agent-nodes/{node.id}/commands/",
        {
            "command_type": "cluster.capi.worker.provision",
            "payload": {"deployment_id": "unbound"},
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="unbound-worker-provisioning",
    )

    assert response.status_code == 403
    assert not AgentCommand.objects.filter(node=node).exists()



@pytest.mark.django_db
def test_target_agent_capi_pivot_postflight_command_is_allowed(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "target-capi-pivot-postflight", "node_type": "target_cluster"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.capi.pivot.postflight",
            "payload": {
                "cluster_name": "edge-lab",
                "namespace": "metal3",
                "baremetal_host_namespace": "metal3-system",
            },
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="target-capi-pivot-postflight",
    )
    assert response.status_code == 202



@pytest.mark.django_db
def test_fleet_inventory_returns_project_scoped_agent_machines_and_hardware(node_workspace):
    user, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {
            "name": "rack-a-node-01",
            "node_type": "eph_node",
            "labels": {
                "hardware_inventory": {
                    "cpu": {"logical_cpus": 64},
                    "memory": {"bytes": 274877906944},
                    "nics": [{"name": "ens1f0", "speed_mbps": 100000}],
                }
            },
        },
        format="json",
    )
    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/"
    )

    assert enrolled.status_code == 201
    assert response.status_code == 200
    assert response.data["count"] == 1
    machine = response.data["results"][0]
    assert machine["name"] == "rack-a-node-01"
    assert machine["identity"]["agent_node_id"] == enrolled.data["id"]
    assert machine["hardware_inventory"]["cpu"]["logical_cpus"] == 64
    assert machine["inventory_source"] == "legacy_agent_label"
    assert response.data["capabilities"]["stable_hardware_identity"] == "not_yet_attested"


@pytest.mark.django_db
def test_fleet_inventory_filters_search_and_paginates(node_workspace):
    _, tenant, project, client = node_workspace
    for name in ["rack-a-01", "rack-a-02", "rack-b-01"]:
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
            {"name": name, "node_type": "target_cluster"},
            format="json",
        )
        assert response.status_code == 201

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/",
        {"search": "rack-a", "limit": 1, "offset": 1, "node_type": "target_cluster"},
    )

    assert response.status_code == 200
    assert response.data["count"] == 2
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["name"] == "rack-a-02"


@pytest.mark.django_db
def test_fleet_inventory_reports_stale_heartbeat_without_mutating_agent_status(node_workspace):
    from django.utils import timezone

    import datetime

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "stale-agent", "node_type": "eph_node"},
        format="json",
    )
    node = AgentNode.objects.get(pk=enrolled.data["id"])
    node.status = AgentNode.Status.ONLINE
    node.last_seen_at = timezone.now() - datetime.timedelta(minutes=5)
    node.save(update_fields=["status", "last_seen_at"])

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/",
        {"status": "stale"},
    )

    node.refresh_from_db()
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["observed_status"] == "stale"
    assert node.status == AgentNode.Status.ONLINE


@pytest.mark.django_db
def test_fleet_inventory_rejects_invalid_filters_and_pagination(node_workspace):
    _, tenant, project, client = node_workspace
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/"

    invalid_status = client.get(url, {"status": "unknown"})
    invalid_page = client.get(url, {"limit": 500})

    assert invalid_status.status_code == 400
    assert invalid_page.status_code == 400



@pytest.mark.django_db
def test_agent_inventory_report_persists_machine_and_keeps_identity_unverified(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "inventory-agent", "node_type": "eph_node"},
        format="json",
    )
    runtime = APIClient()
    report = {
        "status": "observed",
        "scope": "host_sysfs_read_only",
        "system_identity": {
            "sys_vendor": "Acme",
            "product_name": "Rack Server",
            "product_uuid": "ABCDEF00-1111-2222-3333-444455556666",
            "product_serial": "SERIAL-001",
            "source": "host_sysfs_dmi_read_only",
            "attestation": "unverified_agent_report",
        },
        "interfaces": [{"name": "ens1f0", "speed_mbps": 100000}],
    }
    response = runtime.post(
        "/api/v1/agent-runtime/inventory/",
        {"node_id": enrolled.data["id"], "hardware_inventory": report},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )

    assert response.status_code == 200
    machine = AgentMachine.objects.get(agent_node_id=enrolled.data["id"])
    assert machine.tenant == tenant
    assert machine.project == project
    assert machine.vendor == "Acme"
    assert machine.system_uuid == "abcdef00-1111-2222-3333-444455556666"
    assert machine.identity_status == AgentMachine.IdentityStatus.CANDIDATE
    assert machine.inventory_source == "host_sysfs_dmi_read_only"
    assert response.data["attestation"] == "unverified_agent_report"



@pytest.mark.django_db
def test_agent_inventory_preserves_verified_identity_and_detects_material_change(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "verified-inventory-agent", "node_type": "target_cluster"},
        format="json",
    )
    token = enrolled.data["enrollment_token"]
    node_id = enrolled.data["id"]
    runtime = APIClient()
    url = "/api/v1/agent-runtime/inventory/"
    original = {
        "status": "observed",
        "system_identity": {
            "product_uuid": "verified-system-uuid",
            "product_serial": "verified-serial",
        },
    }
    first = runtime.post(
        url,
        {"node_id": node_id, "hardware_inventory": original},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert first.status_code == 200
    machine = AgentMachine.objects.get(agent_node_id=node_id)
    machine.identity_status = AgentMachine.IdentityStatus.VERIFIED
    machine.save(update_fields=["identity_status"])

    unchanged = runtime.post(
        url,
        {"node_id": node_id, "hardware_inventory": original},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    machine.refresh_from_db()
    assert unchanged.status_code == 200
    assert machine.identity_status == AgentMachine.IdentityStatus.VERIFIED

    changed = {
        "status": "observed",
        "system_identity": {
            "product_uuid": "different-system-uuid",
            "product_serial": "different-serial",
        },
    }
    mismatch = runtime.post(
        url,
        {"node_id": node_id, "hardware_inventory": changed},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    machine.refresh_from_db()
    assert mismatch.status_code == 200
    assert machine.identity_status == AgentMachine.IdentityStatus.CONFLICT
    assert machine.identity_fingerprint != ""
    assert any(
        item["type"] == "verified_identity_material_changed"
        for item in machine.identity_conflicts
    )


@pytest.mark.django_db
def test_agent_inventory_cannot_erase_verified_identity_material(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "verified-empty-report-agent", "node_type": "target_cluster"},
        format="json",
    )
    token = enrolled.data["enrollment_token"]
    node_id = enrolled.data["id"]
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=node_id,
        system_uuid="verified-system-uuid",
        identity_material={"product_uuid": "verified-system-uuid"},
        identity_fingerprint="a" * 64,
        identity_status=AgentMachine.IdentityStatus.VERIFIED,
    )
    response = APIClient().post(
        "/api/v1/agent-runtime/inventory/",
        {
            "node_id": node_id,
            "hardware_inventory": {"status": "observed", "system_identity": {}},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    machine.refresh_from_db()
    assert response.status_code == 409
    assert machine.identity_status == AgentMachine.IdentityStatus.VERIFIED
    assert machine.system_uuid == "verified-system-uuid"


@pytest.mark.django_db
def test_agent_inventory_detects_duplicate_hardware_identity_without_merging(node_workspace):
    _, tenant, project, client = node_workspace
    token_by_name = {}
    for name in ("machine-a", "machine-b"):
        enrolled = client.post(
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
            {"name": name, "node_type": "target_cluster"},
            format="json",
        )
        token_by_name[name] = (enrolled.data["id"], enrolled.data["enrollment_token"])
    runtime = APIClient()
    payload = {
        "status": "observed",
        "system_identity": {
            "product_uuid": "same-system-uuid",
            "product_serial": "same-serial",
        },
        "interfaces": [],
    }
    first_id, first_token = token_by_name["machine-a"]
    second_id, second_token = token_by_name["machine-b"]
    for node_id, token in ((first_id, first_token), (second_id, second_token)):
        response = runtime.post(
            "/api/v1/agent-runtime/inventory/",
            {"node_id": node_id, "hardware_inventory": payload},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert response.status_code == 200
    first = AgentMachine.objects.get(agent_node_id=first_id)
    second = AgentMachine.objects.get(agent_node_id=second_id)
    assert first.identity_status == AgentMachine.IdentityStatus.CONFLICT
    assert second.identity_status == AgentMachine.IdentityStatus.CONFLICT
    assert second.identity_conflicts[0]["agent_node_id"] == first_id
    assert first.identity_conflicts[0]["agent_node_id"] == second_id
    assert first.identity_conflicts[0]["matching_fields"] == ["product_serial", "product_uuid"]
    assert AgentMachine.objects.count() == 2


@pytest.mark.django_db
def test_agent_inventory_rejects_invalid_or_revoked_credentials_and_oversized_report(
    node_workspace,
):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "inventory-secure", "node_type": "eph_node"},
        format="json",
    )
    runtime = APIClient()
    url = "/api/v1/agent-runtime/inventory/"
    invalid = runtime.post(
        url,
        {"node_id": enrolled.data["id"], "hardware_inventory": {"status": "observed"}},
        format="json",
        HTTP_AUTHORIZATION="Bearer invalid",
    )
    oversized = runtime.post(
        url,
        {
            "node_id": enrolled.data["id"],
            "hardware_inventory": {"status": "observed", "blob": "x" * 270000},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )
    assert invalid.status_code == 401
    assert oversized.status_code == 413
    revoked = client.post(f"/api/v1/agent-nodes/{enrolled.data['id']}/revoke/", {}, format="json")
    assert revoked.status_code == 200
    rejected = runtime.post(
        url,
        {"node_id": enrolled.data["id"], "hardware_inventory": {"status": "observed"}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )
    assert rejected.status_code == 401



@pytest.mark.django_db
def test_fleet_machine_bmc_config_stores_references_and_keeps_tls_verification(node_workspace):
    from core.models import AuditEvent

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "bmc-config-agent", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
    )
    response = client.put(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {
            "endpoint": "https://192.168.10.20",
            "credential_ref": "vault://lab/bmc/rack-a-01",
            "trust_bundle_ref": "vault://lab/ca/redfish",
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.data["network_check_performed"] is False
    machine.refresh_from_db()
    assert machine.bmc_endpoint == "https://192.168.10.20"
    assert machine.bmc_credential_ref == "vault://lab/bmc/rack-a-01"
    assert machine.bmc_trust_bundle_ref == "vault://lab/ca/redfish"
    assert machine.bmc_tls_verify is True
    assert machine.bmc_identity_evidence["status"] == "not_checked"
    assert "password" not in str(response.data).lower()
    event = AuditEvent.objects.get(
        event_type="fleet.machine.bmc_configured",
        resource_id=str(machine.id),
    )
    assert event.metadata["tls_verify"] is True
    assert "credential_ref" not in event.metadata


@pytest.mark.django_db
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.10.20",
        "https://127.0.0.1",
        "https://localhost",
        "https://169.254.169.254",
        "https://user:pass@192.168.10.20",
        "https://192.168.10.20/redfish/v1",
        "https://192.168.10.20?next=http://127.0.0.1",
    ],
)
def test_fleet_machine_bmc_config_rejects_unsafe_endpoint(node_workspace, endpoint):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "bmc-invalid-agent", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
    )
    response = client.put(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {
            "endpoint": endpoint,
            "credential_ref": "vault://lab/bmc/host",
        },
        format="json",
    )
    assert response.status_code == 400
    machine.refresh_from_db()
    assert machine.bmc_endpoint == ""


@pytest.mark.django_db
def test_fleet_machine_bmc_config_rejects_inline_secrets(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "bmc-secret-agent", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
    )
    response = client.put(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {
            "endpoint": "https://192.168.10.21",
            "credential_ref": "admin-password",
            "password": "not-a-vault-reference",
        },
        format="json",
    )
    assert response.status_code == 400
    machine.refresh_from_db()
    assert machine.bmc_credential_ref == ""



@pytest.mark.django_db
def test_fleet_machine_lifecycle_review_is_audited_and_uses_compare_and_set(node_workspace):
    from core.models import AuditEvent

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "lifecycle-agent", "node_type": "target_cluster"},
        format="json",
    )
    runtime = APIClient()
    ingested = runtime.post(
        "/api/v1/agent-runtime/inventory/",
        {
            "node_id": enrolled.data["id"],
            "hardware_inventory": {
                "status": "observed",
                "system_identity": {"product_uuid": "11111111-2222-3333-4444-555555555555"},
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )
    assert ingested.status_code == 200
    machine = AgentMachine.objects.get(agent_node_id=enrolled.data["id"])
    url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
        f"machines/{machine.id}/lifecycle-review/"
    )
    response = client.post(
        url,
        {
            "expected_state": "discovered",
            "target_state": "identity_review",
            "reason": "Review hardware identity before provisioning.",
        },
        format="json",
    )
    assert response.status_code == 200
    machine.refresh_from_db()
    assert machine.lifecycle_state == "identity_review"
    event = AuditEvent.objects.get(
        event_type="fleet.machine.lifecycle_reviewed",
        resource_id=str(machine.id),
    )
    assert event.metadata["from_state"] == "discovered"
    assert event.metadata["to_state"] == "identity_review"
    stale = client.post(
        url,
        {
            "expected_state": "discovered",
            "target_state": "identity_review",
            "reason": "Attempt transition with stale state.",
        },
        format="json",
    )
    assert stale.status_code == 409


@pytest.mark.django_db
def test_fleet_machine_lifecycle_review_rejects_unverified_provisioning(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "unverified-lifecycle", "node_type": "eph_node"},
        format="json",
    )
    runtime = APIClient()
    runtime.post(
        "/api/v1/agent-runtime/inventory/",
        {
            "node_id": enrolled.data["id"],
            "hardware_inventory": {
                "status": "observed",
                "system_identity": {"product_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )
    machine = AgentMachine.objects.get(agent_node_id=enrolled.data["id"])
    response = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/lifecycle-review/"
        ),
        {
            "expected_state": "discovered",
            "target_state": "ready_for_provisioning",
            "reason": "Attempt provisioning without hardware attestation.",
        },
        format="json",
    )
    assert response.status_code == 409
    assert response.data["code"] == "LIFECYCLE_TRANSITION_NOT_ENABLED"
    machine.refresh_from_db()
    assert machine.lifecycle_state == "discovered"


@pytest.mark.django_db
def test_bmc_redfish_verification_fails_closed_without_credential_resolver(node_workspace, settings):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-no-resolver", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        system_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        identity_material={
            "product_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "product_serial": "SERIAL-RED-01",
        },
        identity_status=AgentMachine.IdentityStatus.CANDIDATE,
        bmc_endpoint="https://192.168.10.30",
        bmc_credential_ref="vault://lab/bmc/host",
    )
    settings.BMC_CREDENTIAL_RESOLVER = ""
    response = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {},
        format="json",
    )
    machine.refresh_from_db()
    assert response.status_code == 503
    assert response.data["verification_status"] == "not_checked"
    assert machine.identity_status == AgentMachine.IdentityStatus.CANDIDATE
    assert machine.bmc_identity_evidence["status"] == "not_checked"


@pytest.mark.django_db
def test_bmc_redfish_verification_rejects_hostname_to_prevent_dns_rebinding(
    node_workspace, settings
):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-hostname", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        bmc_endpoint="https://bmc01.example.net",
        bmc_credential_ref="vault://lab/bmc/host",
    )
    def resolver(**kwargs):
        return {"username": "admin", "password": "secret"}
    settings.BMC_CREDENTIAL_RESOLVER = "tests.test_agent_nodes.resolver_for_redfish_test"
    response = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {},
        format="json",
    )
    assert response.status_code == 400
    machine.refresh_from_db()
    assert machine.identity_status == AgentMachine.IdentityStatus.UNVERIFIED
