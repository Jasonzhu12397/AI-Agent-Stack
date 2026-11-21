"""Fleet refresh applies inspection results the EPH already reported.

Discovery queues ``metal3.host.inspect``; before this, its result was only
consumed by a deployment detail read, so a freshly discovered fleet stayed empty
until someone opened a deployment.
"""

import hashlib
import uuid

import pytest
from core.fleet_inspection import APPLIED_MARKER, sync_fleet_inspections
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
from django.utils import timezone
from rest_framework.test import APIClient


def _workspace():
    user = get_user_model().objects.create_user(
        username="inventory-viewer",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Inspection tenant", slug="inspection-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Inspection project",
        slug="inspection-project",
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
        name="inspection-eph",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(b"inspection-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
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


def _hosts():
    return [
            {
                "bmh_name": "master-0",
                "bmc_host": "10.0.0.11",
                "system_uuid": "4c4c4544-0044-4710-8059-b4c04f4d3332",
                "manufacturer": "Dell",
                "product_name": "PowerEdge R750",
                "pci_addresses": ["0000:3b:00.0"],
                "nics": [
                    {
                        "name": "eno1",
                        "pci_address": "0000:3b:00.0",
                        "mac": "aa:bb:cc:dd:ee:ff",
                    }
                ],
            }
    ]


def _command(tenant, project, node, user, *, command_id=None, hosts=None):
    return AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=node,
        requested_by=user,
        command_type="metal3.host.inspect",
        payload={},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"inspect-{uuid.uuid4()}",
        plan_hash="0" * 64,
        result={
            "status": "succeeded",
            "output": {
                "hosts": _hosts() if hosts is None else hosts,
                "warnings": [],
                "observed_at": "2026-11-20T01:02:03+00:00",
            },
        },
        finished_at=timezone.now(),
    )


@pytest.mark.django_db
def test_fleet_refresh_applies_the_report_without_a_button():
    tenant, project, node, machine, client = _workspace()
    user = TenantMembership.objects.get(tenant=tenant).user
    _command(tenant, project, node, user)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/"
    )

    assert response.status_code == 200
    machine.refresh_from_db()
    assert machine.hardware_inventory["bmh"]["bmh_name"] == "master-0"
    assert machine.hardware_inventory["nics"][0]["pci_address"] == "0000:3b:00.0"
    assert machine.hardware_inventory[APPLIED_MARKER]
    assert machine.inventory_revision == 1


@pytest.mark.django_db
def test_the_same_command_is_not_applied_twice():
    tenant, project, node, machine, client = _workspace()
    user = TenantMembership.objects.get(tenant=tenant).user
    _command(tenant, project, node, user)

    first = sync_fleet_inspections(tenant, project)
    second = sync_fleet_inspections(tenant, project)

    assert first["applied_machines"] == 1
    assert second["applied_machines"] == 0
    machine.refresh_from_db()
    assert machine.inventory_revision == 1


@pytest.mark.django_db
def test_a_failed_inspection_command_is_ignored():
    tenant, project, node, machine, client = _workspace()
    user = TenantMembership.objects.get(tenant=tenant).user
    command = _command(tenant, project, node, user)
    command.status = AgentCommand.Status.FAILED
    command.result = None
    command.save(update_fields=["status", "result"])

    report = sync_fleet_inspections(tenant, project)

    assert report["applied_machines"] == 0
    machine.refresh_from_db()
    assert "bmh" not in machine.hardware_inventory


@pytest.mark.django_db
def test_an_agent_observation_is_never_replaced_by_the_bmh_view():
    tenant, project, node, machine, client = _workspace()
    user = TenantMembership.objects.get(tenant=tenant).user
    machine.hardware_inventory = {
        "nics": [{"name": "eno1", "pci_address": "0000:aa:00.0", "mac": "aa:aa"}],
        "nics_source": "agent",
    }
    machine.inventory_revision = 5
    machine.save(update_fields=["hardware_inventory", "inventory_revision"])
    _command(tenant, project, node, user)

    report = sync_fleet_inspections(tenant, project)

    assert report["pci_adopted"] == 0
    machine.refresh_from_db()
    assert machine.hardware_inventory["nics"][0]["pci_address"] == "0000:aa:00.0"
    assert machine.inventory_revision == 5


@pytest.mark.django_db
def test_a_malformed_result_is_reported_not_merged():
    tenant, project, node, machine, client = _workspace()
    user = TenantMembership.objects.get(tenant=tenant).user
    _command(tenant, project, node, user, hosts="not-a-list")

    report = sync_fleet_inspections(tenant, project)

    assert report["applied_machines"] == 0
    machine.refresh_from_db()
    assert "bmh" not in machine.hardware_inventory
