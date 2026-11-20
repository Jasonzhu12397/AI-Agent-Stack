# ruff: noqa: I001
import pytest
from core.models import (
    AgentMachine,
    AuditEvent,
    FirmwareUpdateJob,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


def fake_resolver(**kwargs):
    return {"username": "admin", "password": "secret"}


@pytest.fixture
def firmware_workspace(db):
    user = get_user_model().objects.create_user(username="firmware-admin", password="test-password")
    tenant = Tenant.objects.create(name="FW tenant", slug="fw-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="FW project", slug="fw-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    node = client_node(user, tenant, project)
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=node,
        vendor="HPE",
        product_name="ProLiant DL380 Gen10",
        bmc_endpoint="https://192.168.10.20",
        bmc_credential_ref="vault://lab/bmc/rack-a-01",
        bmc_trust_bundle_ref="vault://lab/ca/redfish",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, machine, client


def client_node(user, tenant, project):
    from core.models import AgentNode

    return AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="fw-node",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
    )


def base_url(tenant, project, machine):
    return (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/{machine.id}/firmware/"
    )


@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_firmware_updates.fake_resolver")
@pytest.mark.django_db
def test_firmware_inventory_lists_bmc_components(firmware_workspace, monkeypatch):
    _, tenant, project, machine, client = firmware_workspace

    def fake_inventory(machine_arg, credential, trust_bundle_pem):
        assert credential == {"username": "admin", "password": "secret"}
        return {
            "service_enabled": True,
            "service_state": "Enabled",
            "firmware": [
                {
                    "id": "/redfish/v1/UpdateService/FirmwareInventory/BMC",
                    "name": "BMC",
                    "version": "1.2.3",
                    "updateable": True,
                    "status": "OK",
                }
            ],
        }

    monkeypatch.setattr("core.agent_node_views._firmware_inventory", fake_inventory)
    response = client.get(base_url(tenant, project, machine))
    assert response.status_code == 200
    assert response.data["bmc_configured"] is True
    assert response.data["firmware"][0]["version"] == "1.2.3"


@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_firmware_updates.fake_resolver")
@pytest.mark.django_db
def test_firmware_upgrade_requires_confirmation_and_is_idempotent(firmware_workspace, monkeypatch):
    _, tenant, project, machine, client = firmware_workspace
    calls = []

    def fake_update(machine_arg, credential, trust_bundle_pem, image_url, targets, apply_time):
        calls.append(image_url)
        return {
            "task_monitor": "https://192.168.10.20/redfish/v1/TaskService/Tasks/1",
            "task_state": "",
            "message": "submitted",
        }

    monkeypatch.setattr("core.agent_node_views._firmware_simple_update", fake_update)
    payload = {
        "image_url": "https://images.example.com/bmc-2.0.bin",
        "apply_time": "Immediate",
        "confirm": True,
    }
    missing_confirm = dict(payload)
    missing_confirm.pop("confirm")
    response = client.post(
        base_url(tenant, project, machine) + "upgrades/",
        missing_confirm,
        format="json",
        HTTP_IDEMPOTENCY_KEY="fw-key-0001",
    )
    assert response.status_code == 400

    response = client.post(
        base_url(tenant, project, machine) + "upgrades/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="fw-key-0001",
    )
    assert response.status_code == 202
    job_id = response.data["id"]
    assert FirmwareUpdateJob.objects.count() == 1
    assert len(calls) == 1
    event = AuditEvent.objects.get(event_type="fleet.machine.firmware_update_requested")
    assert event.metadata["machine_id"] == str(machine.id)
    assert "images.example.com" in event.metadata["image_host"]

    replay = client.post(
        base_url(tenant, project, machine) + "upgrades/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="fw-key-0001",
    )
    assert replay.status_code == 200
    assert replay.data["id"] == job_id
    assert len(calls) == 1


@pytest.mark.django_db
def test_firmware_upgrade_rejects_invalid_image_url(firmware_workspace):
    _, tenant, project, machine, client = firmware_workspace
    response = client.post(
        base_url(tenant, project, machine) + "upgrades/",
        {"image_url": "http://192.168.10.20/fw.bin", "confirm": True},
        format="json",
        HTTP_IDEMPOTENCY_KEY="fw-key-0002",
    )
    assert response.status_code == 400
    assert "HTTPS" in response.data["detail"]


@pytest.mark.django_db
def test_firmware_upgrade_requires_admin_role(firmware_workspace):
    _, tenant, project, machine, _ = firmware_workspace
    outsider = get_user_model().objects.create_user(
        username="fw-outsider", password="test-password"
    )
    TenantMembership.objects.create(tenant=tenant, user=outsider, role=TenantMembership.Role.VIEWER)
    ProjectMembership.objects.create(
        project=project, user=outsider, role=ProjectMembership.Role.VIEWER
    )
    client = APIClient()
    client.force_authenticate(user=outsider)
    response = client.post(
        base_url(tenant, project, machine) + "upgrades/",
        {"image_url": "https://images.example.com/fw.bin", "confirm": True},
        format="json",
        HTTP_IDEMPOTENCY_KEY="fw-key-0003",
    )
    assert response.status_code == 403


@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_firmware_updates.fake_resolver")
@pytest.mark.django_db
def test_firmware_job_detail_refreshes_task_state(firmware_workspace, monkeypatch):
    user, tenant, project, machine, client = firmware_workspace
    job = FirmwareUpdateJob.objects.create(
        tenant=tenant,
        project=project,
        machine=machine,
        requested_by=user,
        image_url="https://images.example.com/fw.bin",
        idempotency_key="fw-key-0004",
        task_monitor="https://192.168.10.20/redfish/v1/TaskService/Tasks/4",
    )

    def fake_refresh(job_arg):
        job_arg.status = FirmwareUpdateJob.Status.RUNNING
        job_arg.task_state = "Running"
        return job_arg

    monkeypatch.setattr("core.agent_node_views._refresh_firmware_job", fake_refresh)
    response = client.get(base_url(tenant, project, machine) + f"upgrades/{job.id}/")
    assert response.status_code == 200
    assert response.data["status"] == "running"
    assert response.data["task_state"] == "Running"


@pytest.mark.django_db
def test_firmware_vendor_scope_rejects_unsupported_bmc(firmware_workspace):
    _, tenant, project, machine, client = firmware_workspace
    machine.vendor = "Supermicro"
    machine.product_name = "SYS-1029U"
    machine.save(update_fields=["vendor", "product_name"])
    response = client.get(base_url(tenant, project, machine))
    assert response.status_code == 422
    assert response.data["supported_vendors"] == [
        "HPE",
        "Dell",
        "Inspur",
        "H3C",
        "Lenovo",
    ]
