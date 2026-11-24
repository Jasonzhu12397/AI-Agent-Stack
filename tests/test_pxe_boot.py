import json

import httpx
import pytest
from core.bmc_pxe_boot import PxeBootError, pxe_boot_machine
from core.models import (
    AgentMachine,
    AgentNode,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


def _systems():
    return {
        "Members": [{"@odata.id": "/redfish/v1/Systems/1"}],
        "Members@odata.count": 1,
    }


def _system(boot=True):
    document = {
        "UUID": "4c4c4544-0044-4710-8059-b4c04f4d3332",
        "Manufacturer": "Dell",
        "Model": "R750",
        "PowerState": "On",
        "Boot": {
            "BootSourceOverrideEnabled": "Disabled",
            "BootSourceOverrideTarget": "None",
        },
    }
    if boot:
        document["Boot"]["BootSourceOverrideSupported"] = ["None", "Pxe", "Hdd"]
    return document


def _transport(records, boot=True):
    def handler(request):
        records.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": request.content.decode() or None,
            }
        )
        if request.method == "GET" and request.url.path == "/redfish/v1/Systems":
            return httpx.Response(200, json=_systems())
        if request.method == "GET":
            return httpx.Response(200, json=_system(boot=boot))
        if request.method == "PATCH":
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(202, json={"ok": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _credential():
    return {"username": "bmc-user", "password": "bmc-password"}


def test_pxe_boot_plan_only_reads_the_bmc():
    records = []
    evidence = pxe_boot_machine(
        endpoint="https://10.0.0.11/redfish/v1",
        credential=_credential(),
        transport=_transport(records),
        execute=False,
    )
    assert evidence["boot_patch"]["Boot"]["BootSourceOverrideTarget"] == "Pxe"
    assert evidence["boot_patch"]["Boot"]["BootSourceOverrideEnabled"] == "Once"
    assert evidence["reset_type"] == "ForceRestart"
    assert {item["method"] for item in records} == {"GET"}


def test_pxe_boot_execute_sets_once_override_and_resets():
    records = []
    evidence = pxe_boot_machine(
        endpoint="https://10.0.0.11",
        credential=_credential(),
        transport=_transport(records),
        execute=True,
    )
    patch = next(item for item in records if item["method"] == "PATCH")
    assert patch["path"] == "/redfish/v1/Systems/1"
    assert json.loads(patch["body"]) == {
        "Boot": {
            "BootSourceOverrideTarget": "Pxe",
            "BootSourceOverrideEnabled": "Once",
        }
    }
    reset = next(item for item in records if item["method"] == "POST")
    assert reset["path"] == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
    assert json.loads(reset["body"]) == {"ResetType": "ForceRestart"}
    assert evidence["boot_override"] == "Pxe/Once"


def test_pxe_boot_powers_on_a_powered_off_host():
    def handler(request):
        if request.url.path == "/redfish/v1/Systems":
            return httpx.Response(200, json=_systems())
        if request.method == "GET":
            document = _system()
            document["PowerState"] = "Off"
            return httpx.Response(200, json=document)
        if request.method == "PATCH":
            return httpx.Response(204)
        body = json.loads(request.content)
        assert body == {"ResetType": "On"}
        return httpx.Response(204)

    evidence = pxe_boot_machine(
        endpoint="https://10.0.0.11",
        credential=_credential(),
        transport=httpx.MockTransport(handler),
        execute=True,
    )
    assert evidence["reset_type"] == "On"


def test_pxe_boot_rejects_unsupported_boot_targets():
    with pytest.raises(PxeBootError) as excinfo:
        pxe_boot_machine(
            endpoint="https://10.0.0.11",
            credential=_credential(),
            transport=_transport([], boot=False),
        )
    assert excinfo.value.code == "PXE_BOOT_UNSUPPORTED"


def test_pxe_boot_rejects_non_private_bmc():
    with pytest.raises(PxeBootError) as excinfo:
        pxe_boot_machine(
            endpoint="https://8.8.8.8",
            credential=_credential(),
            transport=_transport([]),
        )
    assert excinfo.value.code == "PXE_BOOT_BMC_ADDRESS_INVALID"


def _workspace():
    user = get_user_model().objects.create_user(username="pxe-owner", password="pw")
    tenant = Tenant.objects.create(name="PXE tenant", slug="pxe-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="PXE project", slug="pxe-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="bm-node-1",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="d" * 64,
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=node,
        hardware_inventory={"nics": []},
        bmc_endpoint="https://10.0.0.11",
        bmc_credential_ref="vault/bmc",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, machine, client


def fake_resolver(credential_ref, trust_bundle_ref, tenant_id, project_id):
    return {"username": "bmc", "password": "secret"}


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_pxe_boot.fake_resolver")
def test_pxe_boot_api_plans_without_confirmation(monkeypatch):
    tenant, project, machine, client = _workspace()
    calls = []

    def fake_boot(**kwargs):
        calls.append(kwargs)
        return {
            "available": True,
            "execute": kwargs["execute"],
            "system_path": "/redfish/v1/Systems/1",
            "boot_patch": {
                "Boot": {
                    "BootSourceOverrideTarget": "Pxe",
                    "BootSourceOverrideEnabled": "Once",
                }
            },
            "reset_type": "ForceRestart",
        }

    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", fake_boot)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/pxe-boot/",
        {"machine_ids": [str(machine.id)]},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["execution"] is False
    assert response.data["results"][0]["status"] == "planned"
    assert calls[0]["execute"] is False
    machine.refresh_from_db()
    assert machine.hardware_inventory["pxe_boot"]["execution"] is False


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_pxe_boot.fake_resolver")
def test_pxe_boot_api_requires_confirmation_for_execution(monkeypatch):
    tenant, project, machine, client = _workspace()
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/pxe-boot/",
        {"machine_ids": [str(machine.id)], "execution": True},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["confirmation"] == "PXE_ONCE"


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_pxe_boot.fake_resolver")
def test_pxe_boot_api_executes_and_records_evidence(monkeypatch):
    tenant, project, machine, client = _workspace()
    monkeypatch.setattr(
        "core.agent_node_views.pxe_boot_machine",
        lambda **kwargs: {
            "available": True,
            "execute": True,
            "system_path": "/redfish/v1/Systems/1",
            "reset_type": "ForceRestart",
            "boot_override": "Pxe/Once",
        },
    )
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/pxe-boot/",
        {
            "machine_ids": [str(machine.id)],
            "execution": True,
            "confirm": "PXE_ONCE",
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.data["executed"] == 1
    assert response.data["results"][0]["status"] == "executed"
    machine.refresh_from_db()
    record = machine.hardware_inventory["pxe_boot"]
    assert record["boot_override"] == "Pxe/Once"
    assert record["execution"] is True


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_pxe_boot.fake_resolver")
def test_pxe_boot_refuses_a_retired_machine(monkeypatch):
    tenant, project, machine, client = _workspace()
    machine.lifecycle_state = "retired"
    machine.save(update_fields=["lifecycle_state"])
    monkeypatch.setattr(
        "core.agent_node_views.pxe_boot_machine",
        lambda **kwargs: pytest.fail("a retired machine must never reach the BMC"),
    )

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/pxe-boot/",
        {"machine_ids": [str(machine.id)], "execution": True, "confirm": "PXE_ONCE"},
        format="json",
    )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "MACHINE_NOT_BOOTABLE"
    assert response.data["machines"] == [
        {
            "machine_id": str(machine.id),
            "name": machine.agent_node.name,
            "lifecycle_state": "retired",
        }
    ]


@pytest.mark.django_db
def test_pxe_boot_plan_leaves_retired_machines_out_of_the_default_selection():
    tenant, project, machine, client = _workspace()
    machine.lifecycle_state = "retired"
    machine.save(update_fields=["lifecycle_state"])

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/pxe-boot/",
        {},
        format="json",
    )

    assert response.status_code == 404
