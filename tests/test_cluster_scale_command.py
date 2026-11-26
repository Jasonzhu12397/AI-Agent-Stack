"""The platform only hands cluster.scale to an approved target cluster agent."""

from types import SimpleNamespace

import pytest
from core.agent_node_views import _validate_cluster_scale_result
from core.models import AgentCommand, Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def command_workspace(db):
    user = get_user_model().objects.create_user(username="scale-owner", password="test-password")
    tenant = Tenant.objects.create(name="Scale tenant", slug="scale-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Scale project", slug="scale-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, client


def _payload(**overrides):
    return {
        "target_cluster_name": "edge-lab",
        "machine_deployment": None,
        "target_namespace": "default",
        "desired_workers": 5,
        "execution_mode": "simulation",
        "reason": "add capacity",
        **overrides,
    }


@pytest.mark.django_db
def test_target_cluster_agent_scale_command_waits_for_approval(command_workspace):
    tenant, project, client = command_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "scale-target", "node_type": "target_cluster", "cluster_name": "edge-lab"},
        format="json",
    )
    assert enrolled.status_code == 201
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.scale", "payload": _payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="scale-edge-lab-workers",
    )
    assert response.status_code == 202
    assert response.data["command_type"] == "cluster.scale"
    assert response.data["status"] == AgentCommand.Status.WAITING_APPROVAL


@pytest.mark.django_db
def test_eph_agent_cannot_request_cluster_scale(command_workspace):
    tenant, project, client = command_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "scale-eph", "node_type": "eph_node"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.scale", "payload": _payload()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="scale-eph-rejected",
    )
    assert response.status_code in (400, 403)
    assert response.data["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.django_db
def test_scale_command_rejects_out_of_range_workers(command_workspace):
    tenant, project, client = command_workspace
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "scale-target-invalid", "node_type": "target_cluster", "cluster_name": "edge-lab"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-nodes/{enrolled.data['id']}/commands/",
        {"command_type": "cluster.scale", "payload": _payload(desired_workers=2501)},
        format="json",
        HTTP_IDEMPOTENCY_KEY="scale-edge-lab-invalid",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "CLUSTER_SCALE_REQUEST_INVALID"


def _command(mode="simulation", machine_deployment=None):
    return SimpleNamespace(
        command_type="cluster.scale",
        payload=_payload(execution_mode=mode, machine_deployment=machine_deployment),
    )


def test_scale_result_validation_rejects_fake_success():
    command = _command("simulation")
    result = {
        "cluster_name": "edge-lab",
        "machine_deployment": "workers",
        "desired_workers": 5,
        "approval_required": True,
        "scale_simulated": True,
        "scale_performed": True,
        "side_effects": False,
        "bmh_capacity_sufficient": True,
    }
    assert _validate_cluster_scale_result(command, result) is not None


def test_scale_result_validation_rejects_wrong_machine_deployment():
    command = _command("simulation", machine_deployment="workers")
    result = {
        "cluster_name": "edge-lab",
        "machine_deployment": "other-pool",
        "desired_workers": 5,
        "approval_required": True,
        "scale_simulated": True,
        "scale_performed": False,
        "side_effects": False,
        "bmh_capacity_sufficient": True,
    }
    assert _validate_cluster_scale_result(command, result) is not None


def test_scale_result_validation_accepts_consistent_execute_result():
    command = _command("execute")
    result = {
        "cluster_name": "edge-lab",
        "machine_deployment": "workers",
        "desired_workers": 5,
        "scale_simulated": False,
        "scale_performed": True,
        "side_effects": True,
        "controller_replicas": 5,
        "controller_status": {"replicas": 5, "ready_replicas": 5},
    }
    assert _validate_cluster_scale_result(command, result) is None
