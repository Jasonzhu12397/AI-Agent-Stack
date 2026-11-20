import pytest
from core.models import (
    AgentMachine,
    AgentNode,
    InfrastructureNetworkProfile,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

PROFILE = {
    "networks": [
        {
            "name": "control",
            "role": "control",
            "vlan": 100,
            "mtu": 1500,
            "cidr": "10.0.0.0/24",
            "gateway_ipv4": "10.0.0.1",
            "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.20"}],
            "interface": "eno1",
            "host_roles": ["control_plane"],
        },
        {
            "name": "data",
            "role": "data",
            "vlan": 200,
            "mtu": 9000,
            "cidr": "10.10.0.0/24",
            "gateway_ipv4": "10.10.0.1",
            "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}],
            "interface": "eno2",
            "host_roles": ["worker"],
        },
        {
            "name": "storage",
            "role": "storage-internal",
            "vlan": 400,
            "mtu": 9000,
            "cidr": "10.20.0.0/24",
            "gateway_ipv4": "10.20.0.1",
            "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.20"}],
            "interface": "eno4",
            "host_roles": ["storage"],
        },
    ]
}


@pytest.fixture
def fleet_workspace(db):
    user = get_user_model().objects.create_user(username="fleet-net", password="pw")
    tenant = Tenant.objects.create(name="Fleet net", slug="fleet-net")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="Fleet project", slug="fleet-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="worker-01",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="c" * 64,
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=node,
        hardware_inventory={
            "nics": [
                {"name": "eno2", "mtu": 9000, "vlans": [200], "pci": "0000:3b:00.1"}
            ],
            "switch": {"vlans": [200]},
        },
    )
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant, project=project, profile=PROFILE, updated_by=user
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, machine, client


def _url(tenant, project, machine):
    return (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
        f"fleet/machines/{machine.id}/network/verify/?host_role=worker"
    )


@pytest.mark.django_db
def test_returns_findings_for_machine(fleet_workspace):
    tenant, project, machine, client = fleet_workspace
    response = client.get(_url(tenant, project, machine))
    assert response.status_code == 200
    assert response.data["machine_name"] == "worker-01"
    assert response.data["status"] == "pass"
    assert response.data["checked_networks"] == ["data"]


@pytest.mark.django_db
def test_unknown_host_role_is_rejected(fleet_workspace):
    tenant, project, machine, client = fleet_workspace
    response = client.get(_url(tenant, project, machine).replace("worker", "switch"))
    assert response.status_code == 400


@pytest.mark.django_db
def test_missing_profile_returns_conflict(fleet_workspace):
    tenant, project, machine, client = fleet_workspace
    InfrastructureNetworkProfile.objects.all().delete()
    response = client.get(_url(tenant, project, machine))
    assert response.status_code == 409


@pytest.mark.django_db
def test_viewer_is_forbidden(fleet_workspace):
    tenant, project, machine, _client = fleet_workspace
    user = get_user_model().objects.create_user(username="viewer", password="pw")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.MEMBER
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.VIEWER
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(_url(tenant, project, machine))
    assert response.status_code == 403


def _collect_url(tenant, project):
    return (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/network/collect/"
    )


@pytest.mark.django_db
def test_collect_queues_observation_for_online_agents(fleet_workspace):
    from core.models import AgentCommand
    from django.utils import timezone

    tenant, project, machine, client = fleet_workspace
    second = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=machine.agent_node.created_by,
        name="eph-02",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="d" * 64,
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    offline = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=machine.agent_node.created_by,
        name="worker-offline",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="e" * 64,
        status=AgentNode.Status.OFFLINE,
    )
    response = client.post(
        _collect_url(tenant, project),
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="collect-all-001",
    )
    assert response.status_code == 202
    assert response.data["queued_count"] == 2
    assert {item["node_id"] for item in response.data["queued"]} == {
        str(machine.agent_node.id),
        str(second.id),
    }
    assert AgentCommand.objects.filter(
        tenant=tenant, command_type="host.network.observe"
    ).count() == 2
    assert not AgentCommand.objects.filter(node=offline).exists()

    repeated = client.post(
        _collect_url(tenant, project),
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="collect-all-001",
    )
    assert repeated.status_code == 202
    assert repeated.data["queued_count"] == 0
    assert repeated.data["skipped_count"] == 2


@pytest.mark.django_db
def test_collect_requires_editor(fleet_workspace):
    tenant, project, machine, _client = fleet_workspace
    user = get_user_model().objects.create_user(username="collector", password="pw")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.MEMBER
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.VIEWER
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        _collect_url(tenant, project),
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="collect-viewer-001",
    )
    assert response.status_code == 403
