"""The fleet list is the only surface that can drive per-machine actions.

BMC registration and lifecycle review address an ``AgentMachine`` id, so the
inventory rows have to carry that id plus the states those actions read. Without
this the UI had to guess and could not tell a configured BMC from a missing one.
"""

import hashlib

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
from django.utils import timezone
from rest_framework.test import APIClient


def _workspace(*, bmc_endpoint="", lifecycle_state="discovered"):
    user = get_user_model().objects.create_user(username="fleet-operator", password="test-pw")
    tenant = Tenant.objects.create(name="Fleet tenant", slug="fleet-action-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="Fleet project", slug="fleet-proj")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="fleet-node",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash=hashlib.sha256(b"fleet-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    machine = AgentMachine.objects.create(
        tenant=tenant,
        project=project,
        agent_node=node,
        hardware_inventory={},
        lifecycle_state=lifecycle_state,
        bmc_endpoint=bmc_endpoint,
        bmc_credential_ref="vault/bmc/node" if bmc_endpoint else "",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, node, machine, client


def _row(client, tenant, project):
    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/fleet/machines/"
    )
    assert response.status_code == 200
    return response.json()["results"][0]


@pytest.mark.django_db
def test_rows_carry_the_machine_id_and_lifecycle_state():
    tenant, project, _node, machine, client = _workspace()

    row = _row(client, tenant, project)

    assert row["machine_id"] == str(machine.id)
    assert row["lifecycle_state"] == "discovered"
    assert row["bmc_configured"] is False
    assert row["bmc_host"] is None


@pytest.mark.django_db
def test_rows_report_the_bmc_host_without_leaking_the_reference():
    tenant, project, _node, _machine, client = _workspace(
        bmc_endpoint="https://10.1.2.3/redfish/v1"
    )

    row = _row(client, tenant, project)

    assert row["bmc_configured"] is True
    assert row["bmc_host"] == "10.1.2.3"
    assert "bmc_credential_ref" not in row
    assert "redfish" not in row["bmc_host"]


@pytest.mark.django_db
def test_bmc_configuration_is_visible_after_it_is_saved():
    tenant, project, _node, machine, client = _workspace()

    put = client.put(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
        f"fleet/machines/{machine.id}/bmc-config/",
        {"endpoint": "https://10.1.2.4", "credential_ref": "vault/bmc/new"},
        format="json",
    )

    assert put.status_code == 200
    row = _row(client, tenant, project)
    assert row["bmc_configured"] is True
    assert row["bmc_host"] == "10.1.2.4"


@pytest.mark.django_db
def test_identity_review_moves_the_state_the_list_reports():
    tenant, project, _node, machine, client = _workspace()

    review = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
        f"fleet/machines/{machine.id}/lifecycle-review/",
        {
            "expected_state": "discovered",
            "target_state": "identity_review",
            "reason": "Serial and BMC address match the delivery note.",
        },
        format="json",
    )

    assert review.status_code == 200
    assert _row(client, tenant, project)["lifecycle_state"] == "identity_review"


def _review(client, tenant, project, machine, *, from_state, to_state, reason=None):
    return client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
        f"fleet/machines/{machine.id}/lifecycle-review/",
        {
            "expected_state": from_state,
            "target_state": to_state,
            "reason": reason or "Serial and BMC address match the delivery note.",
        },
        format="json",
    )


@pytest.mark.django_db
def test_the_list_advertises_the_targets_the_contract_allows():
    tenant, project, _node, _machine, client = _workspace(lifecycle_state="identity_review")

    assert _row(client, tenant, project)["lifecycle_targets"] == ["commissioned", "discovered"]


@pytest.mark.django_db
def test_a_commissioned_machine_can_be_retired_end_to_end():
    tenant, project, _node, machine, client = _workspace(lifecycle_state="identity_review")

    assert _review(client, tenant, project, machine, from_state="identity_review",
                   to_state="commissioned").status_code == 200
    assert _row(client, tenant, project)["lifecycle_state"] == "commissioned"
    assert _review(client, tenant, project, machine, from_state="commissioned",
                   to_state="retired").status_code == 200
    assert _row(client, tenant, project)["lifecycle_state"] == "retired"


@pytest.mark.django_db
def test_a_discovered_machine_cannot_be_commissioned_directly():
    tenant, project, _node, machine, client = _workspace()

    response = _review(client, tenant, project, machine, from_state="discovered",
                       to_state="commissioned")

    assert response.status_code == 409
    assert response.json()["code"] == "LIFECYCLE_TRANSITION_NOT_ENABLED"


@pytest.mark.django_db
def test_a_short_review_reason_is_rejected():
    tenant, project, _node, machine, client = _workspace()

    response = _review(client, tenant, project, machine, from_state="discovered",
                       to_state="identity_review", reason="ok")

    assert response.status_code == 400
