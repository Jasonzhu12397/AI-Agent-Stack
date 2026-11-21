"""Discovery is one confirmed action: boot the machines, then queue the inspection.

The chain only queues ``metal3.host.inspect`` when a machine actually booted, and
a machine whose BMC rejected the override is never reported as discovered.
"""

import uuid

import pytest
from core.models import (
    AgentCommand,
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

DISCOVERY = "/api/v1/tenants/{tenant}/projects/{project}/fleet/machines/discovery/"


def fake_resolver(credential_ref, trust_bundle_ref, tenant_id, project_id):
    return {"username": "bmc", "password": "secret"}


def _workspace(*, eph_online=True):
    user = get_user_model().objects.create_user(
        username="discovery-admin",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Discovery tenant", slug="discovery-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Discovery project",
        slug="discovery-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="discovery-node-01",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="a" * 64,
        status=AgentNode.Status.ONLINE if eph_online else AgentNode.Status.OFFLINE,
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
    return tenant, project, node, machine, client


def _fake_boot(status="ok"):
    def boot(**kwargs):
        if status == "failed":
            raise ValueError("boom")
        return {
            "available": True,
            "execute": kwargs["execute"],
            "system_path": "/redfish/v1/Systems/1",
            "boot_patch": {"Boot": {"BootSourceOverrideTarget": "Pxe"}},
            "reset_type": "ForceRestart",
        }

    return boot


def _post(client, tenant, project, body, key="discovery-key"):
    return client.post(
        DISCOVERY.format(tenant=tenant.id, project=project.id),
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_plan_only_boots_nothing_and_queues_nothing(monkeypatch):
    tenant, project, node, machine, client = _workspace()
    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", _fake_boot())

    response = _post(client, tenant, project, {"machine_ids": [str(machine.id)]})

    assert response.status_code == 200
    assert response.data["execution"] is False
    assert response.data["inspect"]["reason"] == "NOT_EXECUTED"
    assert response.data["inspect"]["queued"] == []
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_execution_queues_the_inspection_on_the_eph(monkeypatch):
    tenant, project, node, machine, client = _workspace()
    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", _fake_boot())

    response = _post(
        client,
        tenant,
        project,
        {
            "machine_ids": [str(machine.id)],
            "execution": True,
            "confirm": "PXE_ONCE",
        },
    )

    assert response.status_code == 202
    assert response.data["executed"] == 1
    assert response.data["results"][0]["status"] == "executed"
    queued = response.data["inspect"]["queued"]
    assert len(queued) == 1
    command = AgentCommand.objects.get(id=uuid.UUID(queued[0]["command_id"]))
    assert command.command_type == "metal3.host.inspect"
    assert command.node_id == node.id
    assert command.status == AgentCommand.Status.QUEUED


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_execution_requires_confirmation(monkeypatch):
    tenant, project, node, machine, client = _workspace()
    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", _fake_boot())

    response = _post(
        client,
        tenant,
        project,
        {"machine_ids": [str(machine.id)], "execution": True},
    )

    assert response.status_code == 400
    assert response.data["confirmation"] == "PXE_ONCE"
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_a_failed_boot_never_reports_a_discovery(monkeypatch):
    tenant, project, node, machine, client = _workspace()
    monkeypatch.setattr(
        "core.agent_node_views.pxe_boot_machine",
        _fake_boot(status="failed"),
    )

    response = _post(
        client,
        tenant,
        project,
        {
            "machine_ids": [str(machine.id)],
            "execution": True,
            "confirm": "PXE_ONCE",
        },
    )

    assert response.status_code == 202
    assert response.data["executed"] == 0
    assert response.data["results"][0]["status"] == "failed"
    assert response.data["inspect"]["reason"] == "NO_MACHINE_BOOTED"
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_offline_eph_is_reported_instead_of_silently_skipped(monkeypatch):
    tenant, project, node, machine, client = _workspace(eph_online=False)
    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", _fake_boot())

    response = _post(
        client,
        tenant,
        project,
        {
            "machine_ids": [str(machine.id)],
            "execution": True,
            "confirm": "PXE_ONCE",
        },
    )

    assert response.status_code == 202
    assert response.data["executed"] == 1
    assert response.data["inspect"]["reason"] == "EPH_OFFLINE"
    assert response.data["inspect"]["queued"] == []


@pytest.mark.django_db
@override_settings(BMC_CREDENTIAL_RESOLVER="tests.test_discovery.fake_resolver")
def test_repeating_the_same_key_does_not_queue_twice(monkeypatch):
    tenant, project, node, machine, client = _workspace()
    monkeypatch.setattr("core.agent_node_views.pxe_boot_machine", _fake_boot())
    body = {
        "machine_ids": [str(machine.id)],
        "execution": True,
        "confirm": "PXE_ONCE",
    }

    _post(client, tenant, project, body)
    second = _post(client, tenant, project, body)

    assert second.status_code == 202
    assert second.data["inspect"]["queued"] == []
    assert second.data["inspect"]["skipped"][0]["reason"] == "already_queued"
    assert AgentCommand.objects.count() == 1
