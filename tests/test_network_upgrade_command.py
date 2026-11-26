"""Network bundle upgrades and rollbacks are target-only and result-checked."""

from types import SimpleNamespace

import pytest
from core.agent_node_views import _validate_network_upgrade_result
from core.models import AgentCommand, Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: calico-node
spec:
  replicas: 3
"""


@pytest.fixture
def network_workspace(db):
    user = get_user_model().objects.create_user(username="network-owner", password="test-password")
    tenant = Tenant.objects.create(name="Network tenant", slug="network-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Network project", slug="network-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, client


def _upgrade_payload(**overrides):
    return {
        "target_cluster_name": "edge-lab",
        "target_namespace": "default",
        "plugin": "calico",
        "target_version": "1.1.0",
        "execution_mode": "simulation",
        "documents": MANIFEST,
        "reason": "upgrade bundle",
        **overrides,
    }


@pytest.mark.django_db
def test_network_upgrade_waits_for_approval(network_workspace):
    tenant, project, client = network_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "network-target", "node_type": "target_cluster", "cluster_name": "edge-lab"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "target.network.upgrade", "payload": _upgrade_payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="network-upgrade-calico",
    )
    assert response.status_code == 202
    assert response.data["command_type"] == "target.network.upgrade"
    assert response.data["status"] == AgentCommand.Status.WAITING_APPROVAL


@pytest.mark.django_db
def test_network_rollback_is_target_only_and_approval_gated(network_workspace):
    tenant, project, client = network_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {
            "name": "network-rollback-target",
            "node_type": "target_cluster",
            "cluster_name": "edge-lab",
        },
        format="json",
    )
    acceptable = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "target.network.rollback",
            "payload": {
                "target_cluster_name": "edge-lab",
                "target_namespace": "default",
                "plugin": "calico",
                "execution_mode": "simulation",
                "revision": "20261126T000000Z",
            },
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="network-rollback-calico",
    )
    assert acceptable.status_code == 202
    assert acceptable.data["status"] == AgentCommand.Status.WAITING_APPROVAL
    eph = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "network-eph", "node_type": "eph_node"},
        format="json",
    )
    rejected = client.post(
        f"/api/v1/agent-nodes/{eph.data['id']}/commands/",
        {"command_type": "target.network.rollback", "payload": _upgrade_payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="network-rollback-eph",
    )
    assert rejected.status_code == 400
    assert rejected.data["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.django_db
def test_network_upgrade_rejects_a_foreign_cluster(network_workspace):
    tenant, project, client = network_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "network-foreign", "node_type": "target_cluster", "cluster_name": "edge-lab"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "target.network.upgrade",
            "payload": _upgrade_payload(target_cluster_name="other-cluster"),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="network-upgrade-foreign",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "NETWORK_UPGRADE_CLUSTER_NOT_ENROLLED"


def _command(mode="simulation"):
    return SimpleNamespace(
        command_type="target.network.upgrade",
        payload=_upgrade_payload(execution_mode=mode),
    )


def _base_result(mode):
    return {
        "cluster_name": "edge-lab",
        "namespace": "default",
        "plugin": "calico",
        "target_version": "1.1.0",
        "execution_mode": mode,
        "object_count": 1,
    }


def test_a_simulation_that_claims_to_have_upgraded_is_rejected():
    result = {
        **_base_result("simulation"),
        "approval_required": True,
        "upgrade_simulated": True,
        "upgrade_performed": True,
        "side_effects": False,
    }
    assert _validate_network_upgrade_result(_command(), result) is not None


def test_an_executed_upgrade_without_a_revision_is_rejected():
    result = {
        **_base_result("execute"),
        "upgrade_simulated": False,
        "upgrade_performed": True,
        "side_effects": True,
        "ready_workloads": [{"kind": "Deployment", "name": "calico-node"}],
    }
    assert _validate_network_upgrade_result(_command("execute"), result) is not None


def test_a_consistent_executed_upgrade_is_accepted():
    result = {
        **_base_result("execute"),
        "revision": "20261126T000000Z",
        "upgrade_simulated": False,
        "upgrade_performed": True,
        "side_effects": True,
        "ready_workloads": [{"kind": "Deployment", "name": "calico-node"}],
    }
    assert _validate_network_upgrade_result(_command("execute"), result) is None
