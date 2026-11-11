# ruff: noqa: I001
from node_agent.capabilities import ROLE_CAPABILITIES
import datetime
import hashlib

import httpx
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
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient


def redfish_test_resolver(**kwargs):
    return {"username": "admin", "password": "secret"}


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
def test_eph_and_target_enrollment_credentials_are_independent_and_not_listed(
    node_workspace,
):
    _, tenant, project, client = node_workspace
    eph = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "isolated-eph", "node_type": "eph_node"},
        format="json",
    )
    target = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {
            "name": "isolated-target",
            "node_type": "target_cluster",
            "cluster_name": "target-lab",
        },
        format="json",
    )

    assert eph.status_code == 201
    assert target.status_code == 201
    assert eph.data["id"] != target.data["id"]
    assert eph.data["enrollment_token"] != target.data["enrollment_token"]
    eph_node = AgentNode.objects.get(pk=eph.data["id"])
    target_node = AgentNode.objects.get(pk=target.data["id"])
    assert eph_node.node_type == AgentNode.NodeType.EPH_NODE
    assert target_node.node_type == AgentNode.NodeType.TARGET_CLUSTER
    assert eph_node.credential_hash == hashlib.sha256(
        eph.data["enrollment_token"].encode("utf-8")
    ).hexdigest()
    assert target_node.credential_hash == hashlib.sha256(
        target.data["enrollment_token"].encode("utf-8")
    ).hexdigest()

    runtime = APIClient()
    eph_with_target_token = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": eph.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target.data['enrollment_token']}",
    )
    target_with_eph_token = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": target.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph.data['enrollment_token']}",
    )
    eph_heartbeat = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": eph.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph.data['enrollment_token']}",
    )
    target_heartbeat = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": target.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target.data['enrollment_token']}",
    )

    listed = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
    )
    assert eph_with_target_token.status_code == 401
    assert target_with_eph_token.status_code == 401
    assert eph_heartbeat.status_code == 200
    assert target_heartbeat.status_code == 200
    assert len(listed.data["results"]) == 2
    assert all("enrollment_token" not in item for item in listed.data["results"])
    assert all("credential_hash" not in item for item in listed.data["results"])


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
def test_eph_capi_pivot_command_waits_for_hash_bound_approval_and_is_leased(node_workspace):
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
        {"command_type": "cluster.capi.pivot.execute", "payload": payload},
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
    assert command["command_type"] == "cluster.capi.pivot.execute"
    assert command["lease_token"]


@pytest.mark.django_db
def test_expired_mutating_agent_command_fails_closed_without_redelivery(
    node_workspace,
):
    node_type = "eph_node"
    command_type = "cluster.capi.pivot.execute"
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": f"lease-{node_type}-{command_type}", "node_type": node_type},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": command_type, "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"expired-{node_type}-{command_type}",
    )
    assert created.status_code == 202

    approver = get_user_model().objects.create_user(
        username=f"approver-{node_type}-{command_type}",
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
    assert approved.status_code == 200

    agent_client = APIClient()
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    first_poll = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        **headers,
    )
    assert first_poll.status_code == 200
    assert first_poll.data["command"]["id"] == created.data["id"]

    AgentCommand.objects.filter(pk=created.data["id"]).update(
        lease_expires_at=timezone.now() - datetime.timedelta(seconds=1),
    )
    second_poll = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        **headers,
    )

    command = AgentCommand.objects.get(pk=created.data["id"])
    assert second_poll.status_code == 200
    assert second_poll.data["command"] is None
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == (
        "CAPI_PIVOT_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
        if command_type == "cluster.capi.pivot.execute"
        else "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
    )
    assert command.error["retryable"] is False
    assert command.lease_token is None
    assert command.lease_expires_at is None
    assert command.attempts == 1


def test_cloud_api_command_allowlists_match_node_agent_capability_registry():
    from core.agent_node_views import EPH_COMMANDS, TARGET_COMMANDS

    assert EPH_COMMANDS == ROLE_CAPABILITIES["eph_node"]
    assert TARGET_COMMANDS == ROLE_CAPABILITIES["target_cluster"]


@pytest.mark.django_db
@pytest.mark.parametrize("command_type", ["cluster.provision", "target-agent.bootstrap"])
def test_legacy_cloud_orchestration_commands_are_not_queued_to_node_agent(
    node_workspace,
    command_type,
):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": f"legacy-command-{command_type}", "node_type": "eph_node"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": command_type, "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"reject-{command_type}",
    )

    assert response.status_code == 400
    assert AgentCommand.objects.count() == 0


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
        {"command_type": "cluster.capi.pivot.execute", "payload": {}},
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
    duplicate = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {"node_id": node_id, "outcome": "succeeded", "result": {"ok": True}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )
    conflict = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {"node_id": node_id, "outcome": "succeeded", "result": {"ok": False}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

    assert accepted.status_code == 200
    assert accepted.data["status"] == AgentCommand.Status.SUCCEEDED
    assert duplicate.status_code == 200
    assert duplicate.data["status"] == AgentCommand.Status.SUCCEEDED
    assert conflict.status_code == 409
    assert AgentCommand.objects.get(pk=command["id"]).finished_at is not None


@pytest.mark.django_db
def test_agent_failed_result_replay_is_idempotent(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-failed-result-replay", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "node.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="failed-result-replay",
    )
    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    command = polled.data["command"]
    payload = {
        "node_id": node_id,
        "outcome": "failed",
        "result": {},
        "error": {"code": "NODE_HEALTH_CHECK_FAILED", "retryable": False},
    }
    headers = {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_X_AGENT_LEASE": command["lease_token"],
    }

    accepted = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        payload,
        format="json",
        **headers,
    )
    replay = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        payload,
        format="json",
        **headers,
    )
    conflict_payload = {
        **payload,
        "error": {"code": "DIFFERENT_FAILURE", "retryable": False},
    }
    conflict = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        conflict_payload,
        format="json",
        **headers,
    )

    stored = AgentCommand.objects.get(pk=command["id"])
    assert created.status_code == 202
    assert accepted.status_code == 200
    assert accepted.data["status"] == AgentCommand.Status.FAILED
    assert replay.status_code == 200
    assert replay.data["status"] == AgentCommand.Status.FAILED
    assert conflict.status_code == 409
    assert stored.status == AgentCommand.Status.FAILED
    assert stored.error == payload["error"]
    assert stored.lease_token is None
    assert stored.lease_expires_at is None


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
def test_revoking_node_marks_running_mutating_command_outcome_unknown(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-mutating-revoke", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    token = enrolled.data["enrollment_token"]
    created = client.post(
        f"/api/v1/agent-nodes/{node_id}/commands/",
        {"command_type": "node.health", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="revoke-running-mutating-command",
    )
    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": node_id},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert polled.data["command"]["id"] == created.data["id"]
    AgentCommand.objects.filter(pk=created.data["id"]).update(
        command_type="cluster.capi.pivot.execute",
    )

    revoked = client.post(f"/api/v1/agent-nodes/{node_id}/revoke/", {}, format="json")

    command = AgentCommand.objects.get(pk=created.data["id"])
    assert revoked.status_code == 200
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == (
        "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
    )
    assert command.error["retryable"] is False
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
def test_mutating_command_approval_expires_before_agent_dispatch(node_workspace):
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
            "command_type": "cluster.capi.pivot.execute",
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
@pytest.mark.parametrize(
    "command_type",
    [
        "cluster.control-plane.verify",
        "cluster.provision",
        "target-agent.bootstrap",
    ],
)
def test_agent_poll_rejects_queued_commands_not_allowed_for_node_role(
    node_workspace,
    command_type,
):
    user, tenant, project, _ = node_workspace
    token = f"wrong-role-poll-{command_type}"
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name=f"eph-wrong-role-{command_type}",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=node,
        requested_by=user,
        command_type=command_type,
        payload={},
        status=AgentCommand.Status.QUEUED,
        idempotency_key=f"wrong-role-command-{command_type}",
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
    assert command.lease_token is None
    assert command.lease_expires_at is None



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
def test_bmc_redfish_verification_fails_closed_without_credential_resolver(
    node_workspace, settings
):
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
    settings.BMC_CREDENTIAL_RESOLVER = "test_agent_nodes.redfish_test_resolver"
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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("exception", "expected_status", "reason"),
    [
        (
            PermissionError("credentials rejected"),
            502,
            "authentication_failed",
        ),
        (
            ValueError("unsafe response"),
            400,
            "invalid_response_or_configuration",
        ),
        (httpx.ReadTimeout("BMC timed out"), 504, "request_timeout"),
        (
            httpx.ConnectError("TLS handshake failed"),
            502,
            "connection_or_tls_failure",
        ),
        (RuntimeError("Redfish request failed"), 502, "redfish_request_failed"),
    ],
)
def test_bmc_redfish_api_persists_failure_evidence_and_audit(
    node_workspace, settings, monkeypatch, exception, expected_status, reason
):
    from core.models import AuditEvent

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-failure-agent", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        identity_status=AgentMachine.IdentityStatus.CANDIDATE,
        bmc_endpoint="https://192.168.10.31",
        bmc_credential_ref="vault://lab/bmc/failure-test",
    )
    settings.BMC_CREDENTIAL_RESOLVER = "test_agent_nodes.redfish_test_resolver"

    def fail_verification(*args, **kwargs):
        raise exception

    monkeypatch.setattr("core.agent_node_views._redfish_systems", fail_verification)
    response = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {},
        format="json",
    )

    assert response.status_code == expected_status
    machine.refresh_from_db()
    assert machine.identity_status == AgentMachine.IdentityStatus.CANDIDATE
    assert machine.bmc_identity_evidence["status"] == "error"
    assert machine.bmc_identity_evidence["source"] == "redfish"
    assert machine.bmc_identity_evidence["reason"] == reason
    assert "password" not in str(machine.bmc_identity_evidence).lower()
    event = AuditEvent.objects.get(
        event_type="fleet.machine.bmc_identity_check_failed",
        resource_id=str(machine.id),
    )
    assert event.metadata == {
        "verification_status": "error",
        "reason": reason,
    }
    assert "credential_ref" not in str(event.metadata)
    assert "secret" not in str(event.metadata).lower()


@pytest.mark.django_db
def test_redfish_identity_review_requires_matching_dmi_uuid_and_serial(node_workspace):
    from core.agent_node_views import _redfish_identity_review

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-match", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        system_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        identity_material={
            "product_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "product_serial": "SERIAL-RED-02",
        },
    )
    result = _redfish_identity_review(
        machine,
        [{
            "uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            "serial": "serial-red-02",
            "manufacturer": "Acme",
            "model": "Rack Server",
        }],
    )
    assert result == ("verified", [], "dmi_redfish_match")


@pytest.mark.django_db
def test_redfish_identity_review_rejects_duplicate_machine_identity(node_workspace):
    from core.agent_node_views import _redfish_identity_review

    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-duplicate-a", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        system_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        identity_material={
            "product_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "product_serial": "SERIAL-RED-03",
        },
    )
    other = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-duplicate-b", "node_type": "target_cluster"},
        format="json",
    )
    AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=other.data["id"],
        identity_material={"product_serial": "SERIAL-RED-03"},
    )
    result = _redfish_identity_review(
        machine,
        [{
            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "serial": "SERIAL-RED-03",
            "manufacturer": "Acme",
            "model": "Rack Server",
        }],
    )
    assert result == (
        "conflict",
        ["system_uuid", "product_serial"],
        "duplicate_machine_identity",
    )


@pytest.mark.parametrize(
    ("status_code", "headers", "body", "error_type"),
    [
        (401, {"content-type": "application/json"}, b"{}", PermissionError),
        (403, {"content-type": "application/json"}, b"{}", PermissionError),
        (302, {"location": "https://attacker.invalid"}, b"", ValueError),
        (200, {"content-type": "text/html"}, b"{}", ValueError),
        (200, {"content-type": "application/json"}, b"[]", ValueError),
    ],
)
def test_redfish_read_json_rejects_unsafe_http_responses(
    status_code, headers, body, error_type
):
    import httpx

    from core.agent_node_views import _redfish_read_json

    def handler(request):
        return httpx.Response(status_code, headers=headers, content=body, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error_type):
            _redfish_read_json(
                client,
                "https://192.168.10.30/redfish/v1/Systems",
                httpx.BasicAuth("admin", "secret"),
            )


def test_redfish_read_json_rejects_oversized_response():
    import httpx

    from core.agent_node_views import _redfish_read_json

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b" " * 1048577,
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="size limit"):
            _redfish_read_json(
                client,
                "https://192.168.10.30/redfish/v1/Systems",
                httpx.BasicAuth("admin", "secret"),
            )


def test_redfish_read_json_propagates_timeout():
    import httpx

    from core.agent_node_views import _redfish_read_json

    def handler(request):
        raise httpx.ReadTimeout("BMC read timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            _redfish_read_json(
                client,
                "https://192.168.10.30/redfish/v1/Systems",
                httpx.BasicAuth("admin", "secret"),
            )

 
@pytest.mark.django_db
def test_bmc_api_automatically_uses_builtin_vault_resolver(
    node_workspace, settings, monkeypatch
):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "redfish-vault-auto", "node_type": "target_cluster"},
        format="json",
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node_id=enrolled.data["id"],
        identity_status=AgentMachine.IdentityStatus.CANDIDATE,
        bmc_endpoint="https://192.168.10.32",
        bmc_credential_ref="vault://secret/bmc/rack-a",
    )
    settings.BMC_CREDENTIAL_RESOLVER = ""
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_NAMESPACE = ""
    settings.BMC_VAULT_CA_BUNDLE = ""
    resolved = {}

    def read_secret(client, address, mount, path, headers):
        resolved.update(
            {
                "address": address,
                "mount": mount,
                "path": path,
                "token": headers.get("X-Vault-Token"),
            }
        )
        return {"username": "vault-user", "password": "vault-password"}

    def redfish_systems(endpoint, credential, trust_bundle_pem):
        assert endpoint == machine.bmc_endpoint
        assert credential == {
            "username": "vault-user",
            "password": "vault-password",
        }
        assert trust_bundle_pem == ""
        return [{"uuid": "", "serial": "", "manufacturer": "", "model": ""}]

    monkeypatch.setattr("core.bmc_credentials._read_kv_v2", read_secret)
    monkeypatch.setattr("core.agent_node_views._redfish_systems", redfish_systems)
    response = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/"
            f"machines/{machine.id}/bmc-config/"
        ),
        {},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["verification_status"] == "candidate"
    assert resolved == {
        "address": "https://vault.example.net",
        "mount": "secret",
        "path": f"tenants/{tenant.id}/projects/{project.id}/bmc/rack-a",
        "token": "vault-test-token",
    }



@pytest.mark.django_db
def test_heartbeat_rejects_oversized_payload_without_updating_node(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "heartbeat-size", "node_type": "eph_node"},
        format="json",
    )
    node_id = enrolled.data["id"]
    node_token = enrolled.data["enrollment_token"]
    agent_client = APIClient()

    response = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": node_id, "capabilities": ["x" * 1000 for _ in range(20)]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {node_token}",
    )

    assert response.status_code == 413
    node = AgentNode.objects.get(pk=node_id)
    assert node.status == AgentNode.Status.OFFLINE
    assert node.last_seen_at is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "capabilities",
    [
        ["node.health", "node.health"],
        ["node.health", ""],
        ["x" * 97],
        ["node.health", 7],
        ["node.health\nforged"],
    ],
)
def test_heartbeat_rejects_invalid_capability_identifiers(node_workspace, capabilities):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "heartbeat-capabilities", "node_type": "eph_node"},
        format="json",
    )
    agent_client = APIClient()

    response = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": enrolled.data["id"], "capabilities": capabilities},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )

    assert response.status_code == 400
    node = AgentNode.objects.get(pk=enrolled.data["id"])
    assert node.status == AgentNode.Status.OFFLINE
    assert node.capabilities == []


@pytest.mark.django_db
def test_heartbeat_rejects_invalid_agent_version_without_mutation(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "heartbeat-version", "node_type": "target_cluster"},
        format="json",
    )
    agent_client = APIClient()

    response = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": enrolled.data["id"], "agent_version": {"version": "0.1"}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )

    assert response.status_code == 400
    node = AgentNode.objects.get(pk=enrolled.data["id"])
    assert node.status == AgentNode.Status.OFFLINE
    assert node.agent_version == ""


@pytest.mark.django_db
def test_agent_client_certificate_can_be_bound_and_rotated_by_tenant_admin(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "cert-bound-agent", "node_type": "eph_node"},
        format="json",
    )
    endpoint = f"/api/v1/agent-nodes/{enrolled.data['id']}/client-certificate/"
    first = "a" * 64
    second = "b" * 64

    bound = client.put(endpoint, {"fingerprint_sha256": first}, format="json")
    assert bound.status_code == 200
    assert bound.data["client_certificate"]["fingerprint_sha256"] == first
    assert bound.data["client_certificate"]["status"] == "active"

    rotated = client.put(endpoint, {"fingerprint_sha256": second}, format="json")
    assert rotated.status_code == 200
    node = AgentNode.objects.get(pk=enrolled.data["id"])
    assert node.client_certificate_fingerprint == second
    assert node.client_certificate_revoked_at is None


@pytest.mark.django_db
def test_agent_client_certificate_rejects_invalid_and_duplicate_fingerprints(node_workspace):
    _, tenant, project, client = node_workspace
    ids = []
    for name in ("cert-agent-one", "cert-agent-two"):
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
            {"name": name, "node_type": "target_cluster"},
            format="json",
        )
        ids.append(response.data["id"])
    endpoint = f"/api/v1/agent-nodes/{ids[0]}/client-certificate/"
    invalid = client.put(endpoint, {"fingerprint_sha256": "not-a-fingerprint"}, format="json")
    assert invalid.status_code == 400

    fingerprint = "c" * 64
    bound = client.put(endpoint, {"fingerprint_sha256": fingerprint}, format="json")
    assert bound.status_code == 200
    duplicate = client.put(
        f"/api/v1/agent-nodes/{ids[1]}/client-certificate/",
        {"fingerprint_sha256": fingerprint},
        format="json",
    )
    assert duplicate.status_code == 409
    assert AgentNode.objects.get(pk=ids[1]).client_certificate_fingerprint == ""


@pytest.mark.django_db
def test_agent_client_certificate_can_be_revoked_and_node_goes_offline(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "cert-revoke-agent", "node_type": "target_cluster"},
        format="json",
    )
    endpoint = f"/api/v1/agent-nodes/{enrolled.data['id']}/client-certificate/"
    bound = client.put(
        endpoint, {"fingerprint_sha256": "d" * 64}, format="json"
    )
    assert bound.status_code == 200
    revoked = client.delete(endpoint)
    assert revoked.status_code == 200
    assert revoked.data["client_certificate"]["fingerprint_sha256"] is None
    assert revoked.data["client_certificate"]["status"] == "revoked"
    node = AgentNode.objects.get(pk=enrolled.data["id"])
    assert node.status == AgentNode.Status.OFFLINE
    assert node.client_certificate_revoked_at is not None

    runtime = APIClient()
    rejected = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": enrolled.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
    )
    assert rejected.status_code == 401


@pytest.mark.django_db
@override_settings(
    AGENT_MTLS_TRUSTED_PROXY_IP="192.0.2.10",
    AGENT_MTLS_TRUSTED_PROXY_CIDRS=[],
    AGENT_MTLS_REQUIRED=True,
)
def test_bound_agent_requires_matching_verified_client_certificate(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "cert-auth-agent", "node_type": "target_cluster"},
        format="json",
    )
    endpoint = f"/api/v1/agent-nodes/{enrolled.data['id']}/client-certificate/"
    first_fingerprint = "a" * 64
    rotated_fingerprint = "b" * 64
    assert client.put(
        endpoint,
        {"fingerprint_sha256": first_fingerprint},
        format="json",
    ).status_code == 200

    runtime = APIClient()
    trusted_proxy = "192.0.2.10"
    heartbeat_url = "/api/v1/agent-runtime/heartbeat/"
    payload = {"node_id": enrolled.data["id"]}
    token_header = {"HTTP_AUTHORIZATION": f"Bearer {enrolled.data['enrollment_token']}"}

    spoofed = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR="198.51.100.20",
        HTTP_X_VERIFIED_CLIENT_CERT_SHA256=first_fingerprint,
        **token_header,
    )
    assert spoofed.status_code == 401

    missing = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR=trusted_proxy,
        **token_header,
    )
    assert missing.status_code == 401
    mismatched = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR=trusted_proxy,
        HTTP_X_VERIFIED_CLIENT_CERT_SHA256="c" * 64,
        **token_header,
    )
    assert mismatched.status_code == 401

    with override_settings(
        AGENT_MTLS_TRUSTED_PROXY_IP="",
        AGENT_MTLS_TRUSTED_PROXY_CIDRS=[],
    ):
        unconfigured_proxy = runtime.post(
            heartbeat_url,
            payload,
            format="json",
            REMOTE_ADDR=trusted_proxy,
            HTTP_X_VERIFIED_CLIENT_CERT_SHA256=first_fingerprint,
            **token_header,
        )
    assert unconfigured_proxy.status_code == 401

    accepted = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR=trusted_proxy,
        HTTP_X_VERIFIED_CLIENT_CERT_SHA256=first_fingerprint,
        **token_header,
    )
    assert accepted.status_code == 200

    rotated = client.put(
        endpoint,
        {"fingerprint_sha256": rotated_fingerprint},
        format="json",
    )
    assert rotated.status_code == 200
    old_certificate = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR=trusted_proxy,
        HTTP_X_VERIFIED_CLIENT_CERT_SHA256=first_fingerprint,
        **token_header,
    )
    assert old_certificate.status_code == 401
    new_certificate = runtime.post(
        heartbeat_url,
        payload,
        format="json",
        REMOTE_ADDR=trusted_proxy,
        HTTP_X_VERIFIED_CLIENT_CERT_SHA256=rotated_fingerprint,
        **token_header,
    )
    assert new_certificate.status_code == 200


@pytest.mark.django_db
@override_settings(AGENT_MTLS_REQUIRED=True)
def test_mtls_required_rejects_unbound_agent_even_with_valid_bearer_token(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "unbound-mtls-agent", "node_type": "target_cluster"},
        format="json",
    )
    runtime = APIClient()
    response = runtime.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": enrolled.data["id"]},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {enrolled.data['enrollment_token']}",
        REMOTE_ADDR="192.0.2.10",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_agent_client_certificate_binding_is_tenant_admin_only(node_workspace):
    _, tenant, project, _ = node_workspace
    owner = get_user_model().objects.create_user(username="cert-owner", password="test-password")
    TenantMembership.objects.create(tenant=tenant, user=owner, role=TenantMembership.Role.OWNER)
    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    created = owner_client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "cert-admin-only", "node_type": "eph_node"},
        format="json",
    )
    member = get_user_model().objects.create_user(username="cert-member", password="test-password")
    TenantMembership.objects.create(tenant=tenant, user=member, role=TenantMembership.Role.MEMBER)
    member_client = APIClient()
    member_client.force_authenticate(user=member)
    response = member_client.put(
        f"/api/v1/agent-nodes/{created.data['id']}/client-certificate/",
        {"fingerprint_sha256": "e" * 64},
        format="json",
    )
    assert response.status_code == 403
    assert AgentNode.objects.get(pk=created.data["id"]).client_certificate_fingerprint == ""


@pytest.mark.django_db
def test_agent_command_approval_is_single_writer_and_preserves_first_approver(node_workspace):
    _, tenant, project, client = node_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "approval-race-eph", "node_type": "eph_node"},
        format="json",
    )
    created = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.capi.pivot.execute",
            "payload": {
                "cluster_spec": {
                    "cluster_name": "approval-race",
                    "kubernetes_version": "v1.32.2",
                    "control_plane_replicas": 3,
                    "worker_replicas": 2,
                }
            },
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="approval-race-single-writer",
    )
    assert created.status_code == 202
    assert created.data["status"] == AgentCommand.Status.WAITING_APPROVAL

    approvers = []
    for username in ["approval-first", "approval-second"]:
        user = get_user_model().objects.create_user(
            username=username,
            password="test-password",
        )
        TenantMembership.objects.create(
            tenant=tenant,
            user=user,
            role=TenantMembership.Role.ADMIN,
        )
        approver_client = APIClient()
        approver_client.force_authenticate(user=user)
        approvers.append((user, approver_client))

    first_user, first_client = approvers[0]
    second_user, second_client = approvers[1]
    endpoint = f"/api/v1/agent-commands/{created.data['id']}/approve/"
    payload = {"plan_hash": created.data["plan_hash"]}

    first = first_client.post(endpoint, payload, format="json")
    second = second_client.post(endpoint, payload, format="json")

    assert first.status_code == 200
    assert first.data["status"] == AgentCommand.Status.QUEUED
    assert second.status_code == 409
    command = AgentCommand.objects.get(pk=created.data["id"])
    assert command.status == AgentCommand.Status.QUEUED
    assert command.approved_by_id == first_user.id
    assert command.approved_by_id != second_user.id
    assert command.approved_plan_hash == created.data["plan_hash"]
