# ruff: noqa: I001
import datetime
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

from core.cluster_deployment_views import _validated_spec
from core.cloud_control_bridge import (
    CloudControlNotConfigured,
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


def _observed_network_inventory():
    return {
        "nics": [
            {"name": "eno1", "mtu": 1500, "vlan": 100},
            {"name": "eno2", "mtu": 9000, "vlan": 200},
            {"name": "eno3", "mtu": 9000, "vlan": 300},
        ],
        "bonds": [],
    }


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
    assert spec["bmc_credential_mode"] == "preprovisioned"


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
def test_dispatched_spec_carries_bound_network_evidence(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    machines = [
        _machine(tenant, project, node, index) for index in range(1, 6)
    ]
    evidence = [
        {
            "machine_id": str(machine.id),
            "name": machine.agent_node.name,
            "inventory_revision": machine.inventory_revision,
            "observed_at": machine.observed_at.isoformat()
            if machine.observed_at is not None
            else None,
        }
        for machine in machines[:2]
    ]
    monkeypatch.setattr(
        "core.cluster_deployment_views._network_readiness",
        lambda project, spec: {"ready": True, "evidence": evidence},
    )
    calls = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"created": True, "phase": "not_started", "sequence": 0}

    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        fake_dispatch,
    )
    response = _post(client, tenant, project, node)
    assert response.status_code == 202
    assert calls[0]["spec"]["network_evidence"] == evidence
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    assert deployment.spec["network_evidence"] == evidence


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


def _network_profile():
    return {
        "networks": [
            {
                "name": "control",
                "role": "control",
                "vlan": 100,
                "mtu": 1500,
                "cidr": "10.0.0.0/24",
                "gateway_ipv4": "10.0.0.1",
                "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.50"}],
                "interface": "eno1",
                "host_roles": ["control_plane", "worker"],
            },
            {
                "name": "data",
                "role": "data",
                "vlan": 200,
                "mtu": 9000,
                "cidr": "10.10.0.0/24",
                "gateway_ipv4": "10.10.0.1",
                "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.50"}],
                "interface": "eno2",
                "host_roles": ["worker"],
            },
            {
                "name": "storage",
                "role": "storage-internal",
                "vlan": 300,
                "mtu": 9000,
                "cidr": "10.20.0.0/24",
                "gateway_ipv4": "10.20.0.1",
                "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.50"}],
                "interface": "eno3",
                "host_roles": ["storage"],
            },
        ]
    }


@pytest.mark.django_db
def test_create_inherits_saved_project_network_profile(bridge_workspace, monkeypatch):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_network_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    response = _post(client, tenant, project, node, key="network-profile-001")
    assert response.status_code == 202
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    assert deployment.spec["infra_network_profile"]["networks"][0]["name"] == "control"
    assert (
        deployment.spec["network_spec"]["control_plane_interface"] == "eno1.100"
    )
    assert deployment.spec["data_template"]["network_data"]["networks"]["ipv4"][0]["routes"] == [
        {"destination": "0.0.0.0/0", "gateway": "10.0.0.1"}
    ]
    assert deployment.spec["worker_data_template"]["network_data"]["networks"]["ipv4"]


@pytest.mark.django_db
def test_deployment_gate_blocks_without_network_observation(bridge_workspace):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    response = _post(client, tenant, project, node, key="gate-no-observation")
    assert response.status_code == 409
    assert response.data["network_readiness"]["ready"] is False
    assert response.data["network_readiness"]["control_plane_ready"] == 0
    assert ClusterDeployment.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_deployment_gate_rejects_stale_observation(bridge_workspace):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_network_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now() - datetime.timedelta(hours=48)
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    response = _post(client, tenant, project, node, key="gate-stale")
    assert response.status_code == 409
    assert response.data["network_readiness"]["observed_machines"] == 0


@pytest.mark.django_db
def test_deployment_gate_reports_per_network_findings(bridge_workspace):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        inventory = _observed_network_inventory()
        inventory["nics"][1]["vlan"] = 999
        machine.hardware_inventory = {**machine.hardware_inventory, **inventory}
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    response = _post(client, tenant, project, node, key="gate-findings")
    assert response.status_code == 409
    readiness = response.data["network_readiness"]
    assert readiness["control_plane_ready"] == 5
    assert readiness["worker_ready"] == 0
    worker_findings = [
        finding
        for machine in readiness["machines_with_findings"]
        for finding in machine["roles"]["worker"]["findings"]
    ]
    assert any(finding["code"] == "HOST_VLAN_MISMATCH" for finding in worker_findings)
    assert any(finding["network"] == "data" for finding in worker_findings)


def _sriov_profile():
    profile = _network_profile()
    profile["networks"].append(
        {
            "name": "dpdk",
            "role": "sriov",
            "vlan": 3400,
            "mtu": 9000,
            "cidr": None,
            "interface": "ens3f0",
            "pci_addresses": ["0000:03:00.0"],
            "num_vfs": 8,
            "host_roles": ["worker"],
        }
    )
    return profile


def _observed_sriov_inventory(total_vfs=16):
    inventory = _observed_network_inventory()
    inventory["nics"].append(
        {
            "name": "ens3f0",
            "mtu": 9000,
            "vlan": 3400,
            "pci_address": "0000:03:00.0",
            "sriov_totalvfs": total_vfs,
            "sriov_numvfs": 8,
            "sriov_capable": True,
        }
    )
    return inventory


@pytest.mark.django_db
def test_deployment_gate_accepts_matching_sriov_pci(bridge_workspace, monkeypatch):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_sriov_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_sriov_profile(),
        updated_by=user,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    response = _post(client, tenant, project, node, key="gate-sriov-pass")
    assert response.status_code == 202
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    policy = deployment.spec["sriov_node_policy_manifests"][0]
    assert policy["spec"]["nicSelector"]["rootDevices"] == ["0000:03:00.0"]


@pytest.mark.django_db
def test_deployment_gate_reports_missing_sriov_pci(bridge_workspace):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_sriov_inventory(total_vfs=4),
        }
        machine.hardware_inventory["nics"][-1]["pci_address"] = "0000:04:00.0"
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_sriov_profile(),
        updated_by=user,
    )
    response = _post(client, tenant, project, node, key="gate-sriov-missing")
    assert response.status_code == 409
    readiness = response.data["network_readiness"]
    assert readiness["sriov_ready"] == 0
    assert any(
        finding["code"] == "SRIOV_PCI_ADDRESS_MISSING"
        for finding in readiness["sriov_findings"]
    )


@pytest.mark.django_db
def test_deployment_persists_network_preflight_evidence(bridge_workspace, monkeypatch):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    machines = []
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_network_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.inventory_revision = 3
        machine.save()
        machines.append(machine)
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    response = _post(client, tenant, project, node, key="evidence-persist")
    assert response.status_code == 202
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    preflight = deployment.preflight_result["network_readiness"]
    assert preflight["ready"] is True
    assert len(preflight["evidence"]) == 5
    assert deployment.spec["network_evidence"] == preflight["evidence"]
    assert all(item["inventory_revision"] == 3 for item in preflight["evidence"])


@pytest.mark.django_db
def test_deployment_gate_detects_inventory_drift(bridge_workspace, monkeypatch):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    machines = []
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_network_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.save()
        machines.append(machine)
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    first = _post(client, tenant, project, node, key="evidence-drift")
    assert first.status_code == 202
    changed = machines[0]
    changed.inventory_revision = changed.inventory_revision + 1
    changed.observed_at = timezone.now()
    changed.save()
    second = _post(client, tenant, project, node, key="evidence-drift")
    assert second.status_code == 409
    assert second.data["code"] == "NETWORK_EVIDENCE_DRIFT"
    assert any(
        item["code"] == "NETWORK_EVIDENCE_STALE" for item in second.data["drift"]
    )


@pytest.mark.django_db
def test_network_evidence_verify_endpoint_reports_current(bridge_workspace, monkeypatch):
    from core.models import InfrastructureNetworkProfile

    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        machine = _machine(tenant, project, node, index)
        machine.hardware_inventory = {
            **machine.hardware_inventory,
            **_observed_network_inventory(),
        }
        machine.inventory_source = "host_network_observe"
        machine.observed_at = timezone.now()
        machine.inventory_revision = 2
        machine.save()
    InfrastructureNetworkProfile.objects.create(
        tenant=tenant,
        project=project,
        profile=_network_profile(),
        updated_by=user,
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    created = _post(client, tenant, project, node, key="evidence-verify")
    assert created.status_code == 202
    deployment_id = created.data["id"]
    detail = client.get(f"/api/v1/cluster-deployments/{deployment_id}/")
    assert detail.data["network_evidence_status"]["status"] == "current"
    verified = client.post(f"/api/v1/cluster-deployments/{deployment_id}/network/evidence/verify/")
    assert verified.status_code == 200
    assert verified.data["status"] == "current"
    assert verified.data["checked_machines"] == 5


def _ironic_config():
    return {
        "api_credentials_name": "ironic-auth",
        "version": "37.0",
        "images": {
            "ironic": "registry.local/ironic:37.0",
            "deploy_ramdisk_downloader": "registry.local/ipa-downloader:latest",
            "keepalived": "registry.local/keepalived:2.2.4",
        },
        "networking": {
            "interface": "eno2",
            "ip_address": "10.130.60.84",
        },
    }


@pytest.mark.django_db
def test_invalid_ironic_config_is_rejected_before_dispatch(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    calls = []
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: calls.append(kwargs),
    )
    spec = _cluster_spec()
    spec["ironic"] = _ironic_config()
    del spec["ironic"]["images"]["keepalived"]

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ironic-invalid",
    )

    assert response.status_code == 400
    assert "IRONIC_IMAGE_REQUIRED" in str(response.data)
    assert calls == []
    assert ClusterDeployment.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_dispatch_carries_the_ironic_workload_config(bridge_workspace, monkeypatch):
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
    spec = _cluster_spec()
    spec["ironic"] = _ironic_config()

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ironic-carry",
    )

    assert response.status_code == 202
    assert calls[0]["spec"]["ironic"] == _ironic_config()
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    assert deployment.spec["ironic"] == _ironic_config()
    # The cached plan result may predate the field; the dispatch payload must
    # still carry it on a repeated submission.
    repeated = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="ironic-carry",
    )
    assert repeated.status_code == 200
    assert calls[-1]["spec"]["ironic"] == _ironic_config()


@pytest.mark.django_db
def test_preflight_result_records_the_ironic_workload_gap(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )

    response = _post(client, tenant, project, node, key="preflight-ironic")

    assert response.status_code == 202
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    ironic_workload = deployment.preflight_result["ironic_workload"]
    assert ironic_workload["configured"] is False
    assert ironic_workload["warnings"] == ["IRONIC_WORKLOAD_CR_REQUIRED"]
    detail = client.get(f"/api/v1/cluster-deployments/{deployment.id}/")
    assert detail.data["preflight_result"]["ironic_workload"]["configured"] is False


@pytest.mark.django_db
def test_configured_ironic_workload_has_no_preflight_warning(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    spec = _cluster_spec()
    spec["ironic"] = _ironic_config()

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="preflight-ironic-configured",
    )

    assert response.status_code == 202
    deployment = ClusterDeployment.objects.get(id=response.data["id"])
    ironic_workload = deployment.preflight_result["ironic_workload"]
    assert ironic_workload["configured"] is True
    assert ironic_workload["warnings"] == []


@pytest.mark.django_db
def test_dispatch_spec_is_persisted_so_repeats_cannot_drop_fields(bridge_workspace, monkeypatch):
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

    first = _post(client, tenant, project, node, key="persisted-spec")
    assert first.status_code == 202
    deployment = ClusterDeployment.objects.get(id=first.data["id"])
    assert deployment.plan_result["cloud_control"]["spec"] == calls[0]["spec"]

    repeated = _post(client, tenant, project, node, key="persisted-spec")
    assert repeated.status_code == 200
    assert calls[-1]["spec"] == deployment.plan_result["cloud_control"]["spec"]


class _StubPreflightClient:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error
        self.closed = False

    def get_deployment_action_result(self, deployment_id, action):
        if self._error is not None:
            raise self._error
        assert action == "eph.preflight"
        return {**self._payload, "deployment_id": str(deployment_id)}

    def close(self):
        self.closed = True


@pytest.mark.django_db
def test_eph_preflight_sync_stores_runtime_warnings(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    created = _post(client, tenant, project, node)
    assert created.status_code == 202
    deployment = ClusterDeployment.objects.get(id=created.data["id"])
    stub = _StubPreflightClient(
        payload={
            "available": True,
            "reason": None,
            "status": "blocked",
            "error_code": "EPH_PREFLIGHT_INCOMPLETE",
            "command_id": str(uuid.uuid4()),
            "output": {
                "eph_ready": False,
                "warnings": [
                    "IRONIC_IMAGE_REQUIRED",
                    "IRONIC_IMAGE_REQUIRED",
                    "x" * 200,
                ],
                "observed_at": "2026-11-20T01:02:03+00:00",
            },
        }
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        lambda: stub,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/preflight/eph/sync/",
        {},
        format="json",
    )
    assert response.status_code == 200
    record = response.data["eph_preflight"]
    assert record["available"] is True
    assert record["status"] == "blocked"
    assert record["eph_ready"] is False
    assert record["warnings"][0] == "IRONIC_IMAGE_REQUIRED"
    assert record["warnings"][1] == "x" * 160
    assert len(record["warnings"]) == 2
    assert record["synced_at"]
    deployment.refresh_from_db()
    stored = deployment.preflight_result["eph_preflight"]
    assert stored["warnings"][0] == "IRONIC_IMAGE_REQUIRED"
    # Existing platform-side conclusions must survive the merge.
    assert "network_readiness" in deployment.preflight_result
    assert "ironic_workload" in deployment.preflight_result
    assert stub.closed is True


@pytest.mark.django_db
def test_eph_preflight_sync_reports_missing_run(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    created = _post(client, tenant, project, node)
    deployment = ClusterDeployment.objects.get(id=created.data["id"])
    stub = _StubPreflightClient(
        payload={
            "available": False,
            "reason": "EPH_PREFLIGHT_NOT_RUN",
            "status": None,
            "output": {},
        }
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        lambda: stub,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/preflight/eph/sync/",
        {},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["eph_preflight"]["available"] is False
    assert response.data["eph_preflight"]["reason"] == "EPH_PREFLIGHT_NOT_RUN"
    assert response.data["eph_preflight"]["warnings"] == []


@pytest.mark.django_db
def test_eph_preflight_sync_degrades_without_cloud_control(bridge_workspace, monkeypatch):
    user, tenant, project, node, client = bridge_workspace
    for index in range(1, 6):
        _machine(tenant, project, node, index)
    monkeypatch.setattr(
        "core.cluster_deployment_views.dispatch_deployment",
        lambda **kwargs: {"created": True, "phase": "not_started", "sequence": 0},
    )
    created = _post(client, tenant, project, node)
    deployment = ClusterDeployment.objects.get(id=created.data["id"])

    def fail():
        raise CloudControlNotConfigured("Cloud Control is not configured.")

    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        fail,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/preflight/eph/sync/",
        {},
        format="json",
    )
    assert response.status_code == 503
    assert response.data["code"] == "eph_preflight_unavailable"


def _pxe_sriov_profile():
    return {
        "networks": [
            {
                "name": "ctrl-net",
                "role": "control",
                "interface": "eno1",
                "cidr": "172.18.0.0/24",
                "gateway_ipv4": "172.18.0.1",
                "ip_pools": [
                    {"start": "172.18.0.10", "end": "172.18.0.50", "prefix": 24}
                ],
                "host_roles": ["control_plane"],
            },
            {
                "name": "data-net",
                "role": "data",
                "interface": "eno2",
                "cidr": "10.0.200.0/24",
                "gateway_ipv4": "10.0.200.1",
                "ip_pools": [
                    {"start": "10.0.200.10", "end": "10.0.200.50", "prefix": 24}
                ],
                "host_roles": ["worker"],
            },
            {
                "name": "storage-net",
                "role": "storage-internal",
                "interface": "eno3",
                "cidr": "10.0.10.0/24",
                "gateway_ipv4": "10.0.10.1",
                "ip_pools": [
                    {"start": "10.0.10.10", "end": "10.0.10.50", "prefix": 24}
                ],
                "host_roles": ["storage"],
            },
            {
                "name": "sriov-dp",
                "role": "sriov",
                "interface": "enp55s0f0np0",
                "num_vfs": 16,
                "host_roles": ["worker"],
            },
        ]
    }


@pytest.mark.django_db
def test_sriov_pci_defaults_come_from_observed_host_inventory(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    machine = _machine(tenant, project, node, 1)
    machine.hardware_inventory = {
        **machine.hardware_inventory,
        "nics": [
            {
                "name": "enp55s0f0np0",
                "mac": "aa:bb:cc:dd:ee:01",
                "pci_address": "0000:3b:00.0",
                "sriov_capable": True,
            },
            {
                "name": "enp55s0f0np1",
                "mac": "aa:bb:cc:dd:ee:02",
                "pci_address": "0000:3b:00.1",
                "sriov_capable": True,
            },
        ],
    }
    machine.observed_at = timezone.now()
    machine.save(update_fields=["hardware_inventory", "observed_at"])
    request = type(
        "Request",
        (),
        {
            "data": {
                "cluster_spec": {
                    **_cluster_spec(),
                    "infra_network_profile": _pxe_sriov_profile(),
                }
            }
        },
    )()
    spec, error = _validated_spec(request, project=project)
    assert error is None
    policy = spec["sriov_node_policy_manifests"][0]
    assert policy["spec"]["nicSelector"]["rootDevices"] == ["0000:3b:00.0"]
    assert spec["network_defaults"]["sriov_pci_sources"]["sriov-dp"]["origin"] == (
        "interface_inventory"
    )


@pytest.mark.django_db
def test_sriov_pci_defaults_explicit_values_win(bridge_workspace):
    user, tenant, project, node, client = bridge_workspace
    machine = _machine(tenant, project, node, 1)
    machine.hardware_inventory = {
        **machine.hardware_inventory,
        "nics": [
            {
                "name": "enp55s0f0np0",
                "pci_address": "0000:3b:00.0",
                "sriov_capable": True,
            }
        ],
    }
    machine.observed_at = timezone.now()
    machine.save(update_fields=["hardware_inventory", "observed_at"])
    profile = _pxe_sriov_profile()
    profile["networks"][3]["pci_addresses"] = ["0000:af:00.1"]
    request = type(
        "Request",
        (),
        {"data": {"cluster_spec": {**_cluster_spec(), "infra_network_profile": profile}}},
    )()
    spec, error = _validated_spec(request, project=project)
    assert error is None
    policy = spec["sriov_node_policy_manifests"][0]
    assert policy["spec"]["nicSelector"]["rootDevices"] == ["0000:af:00.1"]
    assert spec["network_defaults"]["sriov_pci_sources"] == {}
