import json

import pytest
from core.models import (
    AgentMachine,
    AgentNode,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.eph_agent.main import execute as eph_execute
from packages.agent_contracts.metal3_bmh_inventory import (
    BmhInventoryError,
    bmc_host_key,
    inspect_bmh_document,
)
from packages.agent_contracts.orchestration import role_for_action


def _bmh(
    name="bmh-1",
    namespace="baremetal-operator-system",
    bmc="redfish+https://10.0.0.11/redfish/v1/Systems/1",
    boot_mac="AA:BB:CC:DD:EE:01",
    nics=None,
    uuid_value="4c4c4544-0044-4710-8059-b4c04f4d3332",
):
    hardware = {
        "systemUUID": uuid_value,
        "systemSerialNumber": "SERIAL-1",
        "manufacturer": "Dell",
        "productName": "PowerEdge R750",
        "powerState": "power on",
        "ramMib": 65536,
        "nics": nics
        if nics is not None
        else [
            {
                "name": "eno1",
                "mac": "aa:bb:cc:dd:ee:01",
                "model": "Broadcom",
                "speedGbps": 25,
                "pxe": True,
            },
            {"name": "eno2", "mac": "aa:bb:cc:dd:ee:02", "model": "Broadcom"},
        ],
    }
    return {
        "apiVersion": "metal3.io/v1alpha1",
        "kind": "BareMetalHost",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "bmc": {"address": bmc, "credentialsName": "bmc-secret"},
            "bootMACAddress": boot_mac,
            "online": True,
        },
        "status": {
            "operationalStatus": "OK",
            "powerState": "poweredOn",
            "provisioning": {"state": "ready"},
            "hardware": hardware,
        },
    }


def test_inspect_normalizes_standard_bmh_and_warns_on_pci():
    parsed = inspect_bmh_document({"items": [_bmh()]})
    host = parsed["hosts"][0]
    assert parsed["host_count"] == 1
    assert host["bmh_name"] == "bmh-1"
    assert host["namespace"] == "baremetal-operator-system"
    assert host["bmc_host"] == "10.0.0.11"
    assert host["boot_mac"] == "aa:bb:cc:dd:ee:01"
    assert host["nics"][0]["name"] == "eno1"
    assert host["nics"][0]["speed_gbps"] == 25
    assert host["pci_addresses"] == []
    assert "BMH_PCI_ADDRESS_UNAVAILABLE" in parsed["warnings"]


def test_inspect_keeps_pci_addresses_when_the_bmh_view_has_them():
    parsed = inspect_bmh_document(
        {
            "items": [
                _bmh(
                    nics=[
                        {
                            "name": "eno1",
                            "mac": "aa:bb:cc:dd:ee:01",
                            "pciAddress": "0000:3b:00.0",
                        }
                    ]
                )
            ]
        }
    )
    host = parsed["hosts"][0]
    assert host["pci_addresses"] == ["0000:3b:00.0"]
    assert parsed["warnings"] == []


def test_inspect_rejects_invalid_and_duplicate_documents():
    with pytest.raises(BmhInventoryError) as excinfo:
        inspect_bmh_document({"items": "not-a-list"})
    assert excinfo.value.code == "BMH_DOCUMENT_INVALID"
    with pytest.raises(BmhInventoryError) as excinfo:
        inspect_bmh_document({"items": [_bmh(), _bmh()]})
    assert excinfo.value.code == "BMH_HOST_DUPLICATE"
    with pytest.raises(BmhInventoryError) as excinfo:
        inspect_bmh_document({"items": [_bmh(name="")]})
    assert excinfo.value.code == "BMH_NAME_REQUIRED"


def test_bmc_host_key_accepts_bare_and_scheme_addresses():
    assert bmc_host_key("https://10.0.0.11/redfish/v1") == "10.0.0.11"
    assert bmc_host_key("10.0.0.11") == "10.0.0.11"
    assert bmc_host_key("") == ""


def test_metal3_host_inspect_is_an_eph_action():
    assert role_for_action("metal3.host.inspect") == "eph"


def test_eph_inspect_action_parses_kubectl_output(monkeypatch):
    monkeypatch.setattr(
        "apps.eph_agent.main._run_kubectl",
        lambda arguments, timeout=60, max_output_bytes=None: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps({"items": [_bmh()]}),
            "stderr": "",
        },
    )
    result = eph_execute({"action": "metal3.host.inspect", "payload": {}})
    assert result["status"] == "succeeded"
    assert result["output"]["host_count"] == 1
    assert result["output"]["warnings"] == ["BMH_PCI_ADDRESS_UNAVAILABLE"]
    assert result["side_effects"] is False


def test_eph_inspect_action_blocks_without_kubectl(monkeypatch):
    monkeypatch.setattr(
        "apps.eph_agent.main._run_kubectl",
        lambda arguments, timeout=60, max_output_bytes=None: {
            "available": False,
            "error": "FileNotFoundError",
        },
    )
    result = eph_execute({"action": "metal3.host.inspect", "payload": {}})
    assert result["status"] == "blocked"
    assert result["error_code"] == "METAL3_BMH_KUBECTL_UNAVAILABLE"


def _workspace():
    user = get_user_model().objects.create_user(
        username="bmh-admin", password="test-pass"
    )
    tenant = Tenant.objects.create(name="BMH tenant", slug="bmh-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="BMH project", slug="bmh-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="bmh-node-1",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=node,
        hardware_inventory={"nics": []},
        bmc_endpoint="https://10.0.0.11",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, machine, client


@pytest.mark.django_db
def test_bmh_inventory_api_matches_and_adopts_pci_inventory():
    tenant, project, machine, client = _workspace()
    document = {
        "items": [
            _bmh(
                nics=[
                    {
                        "name": "eno1",
                        "mac": "aa:bb:cc:dd:ee:01",
                        "pciAddress": "0000:3b:00.0",
                    }
                ]
            )
        ]
    }
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": document},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["matched"] == 1
    assert response.data["unmatched"] == []
    assert response.data["updated"][0]["pci_inventory_adopted"] is True
    machine.refresh_from_db()
    assert machine.hardware_inventory["bmh"]["bmh_name"] == "bmh-1"
    assert machine.hardware_inventory["nics_source"] == "bmh_inspect"
    assert machine.hardware_inventory["nics"][0]["pci_address"] == "0000:3b:00.0"
    assert machine.inventory_source == "bmh_inspect"
    assert machine.observed_at is not None
    assert machine.inventory_revision == 1
    assert machine.bmh_name == "bmh-1"
    assert machine.system_uuid == "4c4c4544-0044-4710-8059-b4c04f4d3332"


@pytest.mark.django_db
def test_bmh_inventory_api_keeps_agent_nics_and_reports_unmatched():
    tenant, project, machine, client = _workspace()
    machine.hardware_inventory = {
        "nics": [{"name": "eno1", "pci_address": "0000:af:00.0"}]
    }
    machine.save(update_fields=["hardware_inventory", "updated_at"])
    document = {
        "items": [
            _bmh(
                nics=[
                    {
                        "name": "eno1",
                        "mac": "aa:bb:cc:dd:ee:01",
                        "pciAddress": "0000:3b:00.0",
                    }
                ]
            ),
            _bmh(name="bmh-unknown", bmc="redfish+https://10.9.9.9/redfish/v1/Systems/1"),
        ]
    }
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": document},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["matched"] == 1
    assert response.data["unmatched"] == ["bmh-unknown"]
    assert response.data["updated"][0]["pci_inventory_adopted"] is False
    machine.refresh_from_db()
    assert machine.hardware_inventory["nics"][0]["pci_address"] == "0000:af:00.0"
    assert machine.inventory_source == "agent_report"
    assert machine.inventory_revision == 0


@pytest.mark.django_db
def test_bmh_inventory_api_rejects_invalid_documents():
    tenant, project, machine, client = _workspace()
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": {"items": "nope"}},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error_code"] == "BMH_DOCUMENT_INVALID"


class _StubActionResultClient:
    def __init__(self, payload=None):
        self._payload = payload
        self.calls = []
        self.closed = False

    def get_deployment_action_result(self, deployment_id, action):
        self.calls.append((str(deployment_id), action))
        return {**self._payload, "deployment_id": str(deployment_id), "action": action}

    def close(self):
        self.closed = True


def _deployment(tenant, project, node, user):
    from core.models import ClusterDeployment

    return ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=node,
        requested_by=user,
        cluster_name="bmh-sync",
        kubernetes_version="v1.37.2",
        idempotency_key="bmh-sync-key",
        preflight_result={"network_readiness": {"ready": False}},
    )


@pytest.mark.django_db
def test_bmh_inventory_sync_pulls_succeeded_inspection(monkeypatch):
    tenant, project, machine, client = _workspace()
    deployment = _deployment(tenant, project, machine.agent_node, client.handler._force_user)
    stub = _StubActionResultClient(
        payload={
            "available": True,
            "reason": None,
            "status": "succeeded",
            "error_code": None,
            "command_id": "7f1b3f9e-0000-0000-0000-000000000000",
            "output": {
                "hosts": [
                    {
                        "bmh_name": "bmh-1",
                        "namespace": "baremetal-operator-system",
                        "bmc_host": "10.0.0.11",
                        "system_uuid": "uuid-1",
                        "pci_addresses": ["0000:3b:00.0"],
                        "nics": [
                            {
                                "name": "eno1",
                                "pci_address": "0000:3b:00.0",
                                "mac": "aa:bb:cc:dd:ee:01",
                            }
                        ],
                    }
                ],
                "warnings": ["BMH_PCI_ADDRESS_UNAVAILABLE"],
                "observed_at": "2026-11-21T01:02:03+00:00",
            },
        }
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        lambda: stub,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/bmh-inventory/sync/",
        {},
        format="json",
    )
    assert response.status_code == 200
    assert stub.calls == [(str(deployment.id), "metal3.host.inspect")]
    record = response.data["bmh_inventory"]
    assert record["matched"] == 1
    assert record["host_count"] == 1
    assert record["observed_at"] == "2026-11-21T01:02:03+00:00"
    assert response.data["updated"][0]["pci_inventory_adopted"] is True
    deployment.refresh_from_db()
    assert deployment.preflight_result["bmh_inventory_sync"]["matched"] == 1
    assert "network_readiness" in deployment.preflight_result
    machine.refresh_from_db()
    assert machine.hardware_inventory["bmh"]["bmh_name"] == "bmh-1"
    assert machine.inventory_source == "bmh_inspect"
    assert stub.closed is True


@pytest.mark.django_db
def test_bmh_inventory_sync_rejects_unsucceeded_and_missing_runs(monkeypatch):
    tenant, project, machine, client = _workspace()
    deployment = _deployment(tenant, project, machine.agent_node, client.handler._force_user)
    blocked = _StubActionResultClient(
        payload={
            "available": True,
            "status": "blocked",
            "error_code": "METAL3_BMH_KUBECTL_UNAVAILABLE",
            "output": {},
        }
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        lambda: blocked,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/bmh-inventory/sync/",
        {},
        format="json",
    )
    assert response.status_code == 409
    assert response.data["error"]["code"] == "bmh_inspect_not_succeeded"
    machine.refresh_from_db()
    assert "bmh" not in machine.hardware_inventory

    missing = _StubActionResultClient(
        payload={"available": False, "reason": "DEPLOYMENT_ACTION_NOT_RUN"}
    )
    monkeypatch.setattr(
        "core.cluster_deployment_views.build_cloud_control_client",
        lambda: missing,
    )
    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/bmh-inventory/sync/",
        {},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["bmh_inventory"]["available"] is False
    assert response.data["bmh_inventory"]["reason"] == "DEPLOYMENT_ACTION_NOT_RUN"


def test_inspect_falls_back_to_the_ipa_inventory_when_bmh_nics_are_empty():
    bmh = _bmh(nics=[])
    bmh["status"]["hardware"]["inventory"] = {
        "nics": [
            {
                "name": "eno2",
                "mac": "aa:bb:cc:dd:ee:02",
                "pciAddress": "0000:3b:00.1",
            }
        ]
    }
    parsed = inspect_bmh_document({"items": [bmh]})

    host = parsed["hosts"][0]
    assert host["nics"][0]["name"] == "eno2"
    assert host["pci_addresses"] == ["0000:3b:00.1"]


@pytest.mark.django_db
def test_bmh_inventory_api_does_not_bump_the_revision_for_the_same_view():
    tenant, project, machine, client = _workspace()
    document = {
        "items": [
            _bmh(
                nics=[
                    {
                        "name": "eno1",
                        "mac": "aa:bb:cc:dd:ee:01",
                        "pciAddress": "0000:3b:00.0",
                    }
                ]
            )
        ]
    }
    first = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": document},
        format="json",
    )
    assert first.data["updated"][0]["pci_inventory_adopted"] is True
    machine.refresh_from_db()
    assert machine.inventory_revision == 1

    second = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": document},
        format="json",
    )
    assert second.status_code == 200
    machine.refresh_from_db()
    assert machine.inventory_revision == 1


@pytest.mark.django_db
def test_bmh_inventory_api_refreshes_a_richer_inspection_of_its_own_view():
    tenant, project, machine, client = _workspace()
    first_document = {
        "items": [
            _bmh(
                nics=[
                    {
                        "name": "eno1",
                        "mac": "aa:bb:cc:dd:ee:01",
                        "pciAddress": "0000:3b:00.0",
                    }
                ]
            )
        ]
    }
    client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": first_document},
        format="json",
    )
    machine.refresh_from_db()
    assert machine.inventory_revision == 1

    second_document = {
        "items": [
            _bmh(
                nics=[
                    {
                        "name": "eno1",
                        "mac": "aa:bb:cc:dd:ee:01",
                        "pciAddress": "0000:3b:00.0",
                    },
                    {
                        "name": "eno2",
                        "mac": "aa:bb:cc:dd:ee:02",
                        "pciAddress": "0000:3b:00.1",
                    },
                ]
            )
        ]
    }
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/bmh-inventory/",
        {"document": second_document},
        format="json",
    )
    assert response.data["updated"][0]["pci_inventory_adopted"] is True
    machine.refresh_from_db()
    assert [nic["name"] for nic in machine.hardware_inventory["nics"]] == ["eno1", "eno2"]
    assert machine.inventory_revision == 2
