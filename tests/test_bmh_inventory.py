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
