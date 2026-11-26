"""The platform gates repair commands and rejects a fake repair result."""

from types import SimpleNamespace

import pytest
from core.agent_node_views import _validate_cluster_repair_result
from core.models import AgentCommand, Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def repair_workspace(db):
    user = get_user_model().objects.create_user(username="repair-owner", password="test-password")
    tenant = Tenant.objects.create(name="Repair tenant", slug="repair-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Repair project", slug="repair-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, client


def _payload(**overrides):
    return {
        "target_cluster_name": "edge-lab",
        "target_namespace": "default",
        "machine_deployment": "workers",
        "execution_mode": "simulation",
        "reason": "replace unhealthy worker",
        **overrides,
    }


@pytest.mark.django_db
def test_repair_requires_target_cluster_and_approval(repair_workspace):
    tenant, project, client = repair_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "repair-target", "node_type": "target_cluster", "cluster_name": "edge-lab"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.worker.repair", "payload": _payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="repair-edge-lab-worker",
    )
    assert response.status_code == 202
    assert response.data["command_type"] == "cluster.worker.repair"
    assert response.data["status"] == AgentCommand.Status.WAITING_APPROVAL


@pytest.mark.django_db
def test_eph_agent_cannot_request_repair(repair_workspace):
    tenant, project, client = repair_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "repair-eph", "node_type": "eph_node"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.worker.repair", "payload": _payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="repair-eph-rejected",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.django_db
def test_repair_rejects_a_foreign_cluster(repair_workspace):
    tenant, project, client = repair_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {
            "name": "repair-target-foreign",
            "node_type": "target_cluster",
            "cluster_name": "edge-lab",
        },
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {
            "command_type": "cluster.worker.repair",
            "payload": _payload(target_cluster_name="another-cluster"),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="repair-foreign-cluster",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "CLUSTER_REPAIR_CLUSTER_NOT_ENROLLED"


def _command(mode="simulation"):
    return SimpleNamespace(
        command_type="cluster.worker.repair",
        payload=_payload(execution_mode=mode),
    )


def test_a_simulation_that_claims_to_have_deleted_is_rejected():
    result = {
        "cluster_name": "edge-lab",
        "namespace": "default",
        "machine_deployment": "workers",
        "unhealthy_machines": ["worker-2"],
        "selected_machine": "worker-2",
        "approval_required": True,
        "repair_simulated": True,
        "repair_performed": True,
        "side_effects": False,
    }
    assert _validate_cluster_repair_result(_command(), result) is not None


def test_an_executed_repair_must_report_the_deleted_machine():
    result = {
        "cluster_name": "edge-lab",
        "namespace": "default",
        "machine_deployment": "workers",
        "unhealthy_machines": ["worker-2"],
        "selected_machine": "worker-2",
        "repair_simulated": False,
        "repair_performed": True,
        "side_effects": True,
        "deleted_machines": [],
    }
    assert _validate_cluster_repair_result(_command("execute"), result) is not None


def test_a_consistent_executed_repair_is_accepted():
    result = {
        "cluster_name": "edge-lab",
        "namespace": "default",
        "machine_deployment": "workers",
        "unhealthy_machines": ["worker-2"],
        "selected_machine": "worker-2",
        "repair_simulated": False,
        "repair_performed": True,
        "side_effects": True,
        "deleted_machines": ["worker-2"],
    }
    assert _validate_cluster_repair_result(_command("execute"), result) is None
