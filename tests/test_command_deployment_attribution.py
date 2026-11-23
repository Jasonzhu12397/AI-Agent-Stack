"""A command records the cluster it acts on, so deployment-scoped retention works.

The generic command view used to create rollbacks and upgrades without binding
the command to its deployment. Retention protection filters rollbacks by
deployment, so an unattributed command could never hold the snapshot it named,
and the post-change backup trigger was skipped by the same missing link.
"""

from __future__ import annotations

import importlib

import pytest
from core.etcd_backup_policy import protected_names
from core.etcd_backup_queue import backup_payload
from core.etcd_backup_state import etcd_backup_state
from core.models import (
    AgentCommand,
    AgentNode,
    ClusterDeployment,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

ROLLBACK_ACTION = "cluster.kubernetes.version.rollback"
UPGRADE_ACTION = "cluster.kubernetes.version.upgrade"
SNAPSHOT_NAME = "snapshot-pre-edge-lab-v1.35.0-20261121T000000Z.db"
SNAPSHOT_PATH = f"/var/lib/etcd/backups/{SNAPSHOT_NAME}"


def _workspace():
    user = get_user_model().objects.create_user(
        username="attribution-admin", password="x"
    )
    tenant = Tenant.objects.create(name="Attribution tenant", slug="attribution-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Attribution project", slug="attribution-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-attribution",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-attribution",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash="t" * 64,
    )
    deployment = ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=eph,
        target_node=target,
        requested_by=user,
        cluster_name="edge-lab",
        kubernetes_version="v1.34.2",
        idempotency_key="attribution-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec={},
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, eph, target, deployment, client


def _post(client, node, command_type, payload, key):
    return client.post(
        f"/api/v1/agent-nodes/{node.id}/commands/",
        {"command_type": command_type, "payload": payload},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


@pytest.mark.django_db
def test_rollback_created_via_view_binds_its_deployment_and_protects_snapshot():
    user, tenant, project, eph, target, deployment, client = _workspace()

    response = _post(
        client,
        target,
        ROLLBACK_ACTION,
        {
            "cluster_name": "edge-lab",
            "snapshot_path": SNAPSHOT_PATH,
            "execution_mode": "simulation",
        },
        "rb-attribution-1",
    )

    assert response.status_code == 202
    command = AgentCommand.objects.get(idempotency_key="rb-attribution-1")
    assert command.deployment_id == deployment.id

    # The protection list, the queued backup payload and the panel state all
    # read the same deployment-scoped link.
    assert protected_names(deployment) == [SNAPSHOT_NAME]
    assert backup_payload(deployment)["protect"] == (SNAPSHOT_NAME,)
    assert etcd_backup_state(deployment)["policy"]["protect"] == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_upgrade_created_via_view_binds_its_deployment():
    user, tenant, project, eph, target, deployment, client = _workspace()

    response = _post(
        client,
        target,
        UPGRADE_ACTION,
        {
            "cluster_name": "edge-lab",
            "current_version": "v1.34.2",
            "target_version": "v1.35",
            "execution_mode": "simulation",
        },
        "up-attribution-1",
    )

    assert response.status_code == 202
    command = AgentCommand.objects.get(idempotency_key="up-attribution-1")
    assert command.deployment_id == deployment.id


@pytest.mark.django_db
def test_ended_rollback_commands_release_their_snapshot():
    user, tenant, project, eph, target, deployment, client = _workspace()

    def _ended(status: str, key: str) -> None:
        AgentCommand.objects.create(
            tenant=tenant,
            project=project,
            node=target,
            requested_by=user,
            command_type=ROLLBACK_ACTION,
            payload={"snapshot_path": SNAPSHOT_PATH},
            status=status,
            idempotency_key=key,
            plan_hash="0" * 64,
            deployment=deployment,
        )

    _ended(AgentCommand.Status.SUCCEEDED, "rb-succeeded")
    _ended(AgentCommand.Status.FAILED, "rb-failed")
    _ended(AgentCommand.Status.CANCELLED, "rb-cancelled")

    assert protected_names(deployment) == []


@pytest.mark.django_db
def test_open_rollback_commands_keep_protecting_their_snapshot():
    user, tenant, project, eph, target, deployment, client = _workspace()

    for index, status in enumerate(
        (
            AgentCommand.Status.WAITING_APPROVAL,
            AgentCommand.Status.QUEUED,
            AgentCommand.Status.RUNNING,
        )
    ):
        AgentCommand.objects.create(
            tenant=tenant,
            project=project,
            node=target,
            requested_by=user,
            command_type=ROLLBACK_ACTION,
            payload={"snapshot_path": SNAPSHOT_PATH},
            status=status,
            idempotency_key=f"rb-open-{index}",
            plan_hash="0" * 64,
            deployment=deployment,
        )

    assert protected_names(deployment) == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_legacy_null_deployment_rollback_is_backfilled_by_migration():
    user, tenant, project, eph, target, deployment, client = _workspace()
    legacy = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=ROLLBACK_ACTION,
        payload={"snapshot_path": SNAPSHOT_PATH},
        status=AgentCommand.Status.QUEUED,
        idempotency_key="rb-legacy",
        plan_hash="0" * 64,
        deployment=None,
    )

    migration = importlib.import_module(
        "core.migrations.0033_backfill_command_deployment"
    )
    migration.backfill_command_deployment(django_apps, None)

    legacy.refresh_from_db()
    assert legacy.deployment_id == deployment.id
    assert protected_names(deployment) == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_a_node_without_a_deployment_stays_an_empty_attribution():
    from core.agent_node_views import _deployment_for_node

    user, tenant, project, eph, target, deployment, client = _workspace()

    # An EPH node has no cluster of its own; the empty link is a legal state,
    # not a missing lookup.
    assert _deployment_for_node(eph) is None
