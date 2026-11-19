# ruff: noqa: I001
import hashlib
import uuid

import pytest
from core.models import (
    AgentMachine,
    AgentNode,
    ClusterDeployment,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.cloud_control_bridge import (
    FleetInventoryEmpty,
    build_cloud_control_spec,
    control_identity,
)


@pytest.fixture
def bridge_workspace(db):
    user = get_user_model().objects.create_user(
        username="bridge-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Bridge tenant", slug="bridge-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Bridge project",
        slug="bridge-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    token = "bridge-node-token"
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="bridge-eph",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, node, client


def _cluster_spec():
    return {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.37.2",
        "control_plane_replicas": 3,
        "worker_replicas": 2,
    }


def _machine(tenant, project, node, index):
    machine_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=node.created_by,
        name=f"machine-node-{index}",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash=hashlib.sha256(f"machine-{index}".encode()).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    return AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=machine_node,
        system_uuid=f"system-{index}",
        identity_status=AgentMachine.IdentityStatus.VERIFIED,
        bmc_endpoint=f"10.0.0.{index}/redfish/v1",
        bmc_credential_ref=f"bmc-{index}",
        hardware_inventory={
            "boot_mac": f"00:11:22:33:44:{index:02d}",
            "cpu_cores": 16,
            "ram_mib": 32768,
            "disk_gib": 480,
        },
    )


def _post(client, tenant, project, node, key="bridge-plan-001"):
    return client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": _cluster_spec()},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def test_control_identity_is_stable_and_scoped():
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    node_id = uuid.uuid4()
    first = control_identity(tenant_id, project_id, node_id, "edge-lab")
    second = control_identity(tenant_id, project_id, node_id, "edge-lab")
    other_cluster = control_identity(tenant_id, project_id, node_id, "edge-lab-2")
    assert first == second
    assert first[1] != other_cluster[1]


@pytest.mark.django_db
def test_build_spec_uses_platform_inventory(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    spec = build_cloud_control_spec(
        _cluster_spec(),
        project_id=project.id,
        cluster_name="edge-lab",
    )
    assert spec["control_plane_count"] == 3
    assert spec["k8s_version"] == "v1.37.2"
    assert len(spec["fleet"]) == 5
    assert spec["fleet"][0]["secret_ref"]


@pytest.mark.django_db
def test_build_spec_fails_on_empty_fleet(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    with pytest.raises(FleetInventoryEmpty):
        build_cloud_control_spec(
            _cluster_spec(),
            project_id=project.id,
            cluster_name="edge-lab",
        )


@pytest.mark.django_db
def test_create_dispatches_to_cloud_control_idempotently(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    calls = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"created": True, "phase": "not_started", "sequence": 0}

    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        fake_dispatch,
    )
    first = _post(client, tenant, project, node)
    repeated = _post(client, tenant, project, node)
    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.data["id"] == first.data["id"]
    assert ClusterDeployment.objects.filter(
        tenant=tenant,
        idempotency_key="bridge-plan-001",
    ).count() == 1
    deployment = ClusterDeployment.objects.get(id=first.data["id"])
    assert deployment.status == ClusterDeployment.Status.PROVISIONING
    control = deployment.plan_result["cloud_control"]
    assert len(control["spec"]["fleet"]) == 5
    assert len(calls) == 2
    assert calls[0]["deployment_id"] == deployment.id
    assert calls[0]["spec"] == calls[1]["spec"]


@pytest.mark.django_db
def test_create_without_fleet_creates_no_deployment(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    response = _post(client, tenant, project, node)
    assert response.status_code == 409
    assert response.data["code"] == "FLEET_INVENTORY_EMPTY"
    assert ClusterDeployment.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_create_reports_unconfigured_cloud_control(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    response = _post(client, tenant, project, node)
    assert response.status_code == 503
    assert response.data["code"] == "CLOUD_CONTROL_NOT_CONFIGURED"


@pytest.mark.django_db
def test_create_rejects_insufficient_fleet(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 4):
        _machine(tenant, project, node, index)
    response = _post(client, tenant, project, node)
    assert response.status_code == 409
    assert response.data["code"] == "FLEET_INVENTORY_INSUFFICIENT"
    assert ClusterDeployment.objects.filter(tenant=tenant).count() == 0


def test_legacy_handoff_and_checkpoint_routes_are_gone():
    from django.urls import Resolver404, resolve

    with pytest.raises(Resolver404):
        resolve(f"/api/v1/cluster-deployments/{uuid.uuid4()}/target-agent/handoff/")
    with pytest.raises(Resolver404):
        resolve(f"/api/v1/agent-runtime/deployments/{uuid.uuid4()}/checkpoint/")


@pytest.mark.django_db
def test_create_validates_sriov_worker_config(bridge_workspace, monkeypatch):
    from core.cloud_control_bridge import build_cloud_control_spec as real_build

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 4):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_spec",
        real_build,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True},
    )
    spec = _cluster_spec()
    spec["worker_provisioning"] = {
        "sriov_device_plugin": {
            "pools": {
                "dpdk": {
                    "device_type": "netdevice",
                    "pci_addresses": ["invalid"],
                }
            }
        }
    }
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="sriov-invalid",
    )
    assert response.status_code == 400
    assert ClusterDeployment.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_create_rejects_control_plane_size_the_planner_cannot_build(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    spec = _cluster_spec()
    spec["control_plane_replicas"] = 9
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="cp-nine",
    )
    assert response.status_code == 400
    assert "1, 3, 5, 7" in response.data["detail"]
