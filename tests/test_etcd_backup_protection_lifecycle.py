"""Lease-based snapshot protection for rollback commands.

A rollback protects the snapshot it will restore only while it can still run.
An approved command that no agent ever claimed, and whose agent is gone for
good, must not hold the snapshot forever: the sweep moves it to a terminal
``expired`` state, releases the protection and writes an audit event. Commands
that are running, or still inside the protection horizon, keep protecting.

The backup command's ``protect`` set is recomputed when the agent claims it, so
a rollback created after the backup was enqueued is still covered instead of
being missed because the enqueued payload froze an empty set.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from core.etcd_backup_policy import (
    DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES,
    ROLLBACK_PROTECTION_RELEASED_EVENT,
    protected_names,
    release_expired_rollback_protection,
    rollback_protection_horizon_minutes,
)
from core.etcd_backup_queue import queue_backup
from core.models import (
    AgentCommand,
    AgentNode,
    AuditEvent,
    ClusterDeployment,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

ROLLBACK_ACTION = "cluster.kubernetes.version.rollback"
SNAPSHOT_NAME = "snapshot-pre-edge-lab-v1.35.0-20261121T000000Z.db"
SNAPSHOT_PATH = f"/var/lib/etcd/backups/{SNAPSHOT_NAME}"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
NODE_TOKEN = "protection-lifecycle-token"


def snapshot_path_name(command: AgentCommand) -> str:
    return str(command.payload.get("snapshot_path") or "")


def _workspace(spec: dict | None = None):
    user = get_user_model().objects.create_user(
        username="protection-admin", password="x"
    )
    tenant = Tenant.objects.create(
        name="Protection tenant", slug="protection-tenant"
    )
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Protection project", slug="protection-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-protection",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-protection",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(NODE_TOKEN.encode("utf-8")).hexdigest(),
    )
    deployment = ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=AgentNode.objects.get(name="eph-protection"),
        target_node=target,
        requested_by=user,
        cluster_name="edge-lab",
        kubernetes_version="v1.34.2",
        idempotency_key="protection-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec=spec or {},
    )
    return user, tenant, project, target, deployment


def _rollback(
    deployment,
    *,
    status: str,
    approved_at: datetime | None,
    key: str,
    lease_token=None,
    snapshot_path: str = SNAPSHOT_PATH,
) -> AgentCommand:
    return AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        node=deployment.target_node,
        requested_by=deployment.requested_by,
        command_type=ROLLBACK_ACTION,
        payload={"snapshot_path": snapshot_path},
        status=status,
        idempotency_key=key,
        plan_hash="0" * 64,
        approved_at=approved_at,
        approved_plan_hash="0" * 64,
        lease_token=lease_token,
        deployment=deployment,
    )


@pytest.mark.django_db
def test_approved_but_unclaimed_rollback_releases_after_the_horizon():
    user, tenant, project, target, deployment = _workspace()
    stale = NOW - timedelta(
        minutes=DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES + 1
    )
    rollback = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=stale,
        key="rb-stale-unclaimed",
    )

    released = release_expired_rollback_protection(now=NOW)

    assert rollback.id in released
    rollback.refresh_from_db()
    assert rollback.status == AgentCommand.Status.EXPIRED
    assert rollback.finished_at is not None
    assert rollback.lease_token is None
    # The snapshot is no longer held once the command left the live set.
    assert protected_names(deployment) == []

    event = AuditEvent.objects.get(
        event_type=ROLLBACK_PROTECTION_RELEASED_EVENT,
        resource_type="agent_command",
        resource_id=str(rollback.id),
    )
    assert event.metadata["deployment_id"] == str(deployment.id)
    assert event.metadata["snapshot_path"] == snapshot_path_name(rollback)
    assert event.metadata["protection_horizon_minutes"] == (
        DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES
    )


@pytest.mark.django_db
def test_running_and_fresh_rollbacks_keep_protecting_during_sweep():
    user, tenant, project, target, deployment = _workspace()
    old = NOW - timedelta(
        minutes=DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES + 10
    )
    running = _rollback(
        deployment,
        status=AgentCommand.Status.RUNNING,
        approved_at=old,
        key="rb-running-old",
        lease_token="11111111-1111-1111-1111-111111111111",
    )
    fresh = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=NOW - timedelta(minutes=30),
        key="rb-fresh-queued",
    )
    waiting = _rollback(
        deployment,
        status=AgentCommand.Status.WAITING_APPROVAL,
        approved_at=None,
        key="rb-waiting",
    )

    released = release_expired_rollback_protection(now=NOW)

    assert released == []
    for command in (running, fresh, waiting):
        command.refresh_from_db()
    assert running.status == AgentCommand.Status.RUNNING
    assert fresh.status == AgentCommand.Status.QUEUED
    assert waiting.status == AgentCommand.Status.WAITING_APPROVAL
    # The running restore and the still-live candidates still hold the file.
    assert protected_names(deployment) == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_sweep_transitions_and_audits_exactly_once():
    user, tenant, project, target, deployment = _workspace()
    stale = NOW - timedelta(
        minutes=DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES + 1
    )
    rollback = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=stale,
        key="rb-idempotent",
    )

    first = release_expired_rollback_protection(now=NOW)
    second = release_expired_rollback_protection(now=NOW)

    assert rollback.id in first
    assert second == []
    rollback.refresh_from_db()
    assert rollback.status == AgentCommand.Status.EXPIRED
    assert AuditEvent.objects.filter(
        event_type=ROLLBACK_PROTECTION_RELEASED_EVENT,
        resource_id=str(rollback.id),
    ).count() == 1


@pytest.mark.django_db
def test_deployment_spec_can_shorten_the_protection_horizon():
    user, tenant, project, target, deployment = _workspace(
        spec={"kubernetes": {"etcd_backup_rollback_protection_minutes": 30}}
    )
    assert rollback_protection_horizon_minutes(deployment) == 30

    approved_at = NOW - timedelta(minutes=31)
    rollback = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=approved_at,
        key="rb-custom-horizon",
    )

    release_expired_rollback_protection(now=NOW)

    rollback.refresh_from_db()
    assert rollback.status == AgentCommand.Status.EXPIRED

    fresh = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=NOW - timedelta(minutes=20),
        key="rb-custom-fresh",
    )
    release_expired_rollback_protection(now=NOW)
    fresh.refresh_from_db()
    assert fresh.status == AgentCommand.Status.QUEUED
    assert protected_names(deployment) == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_rollback_created_after_enqueue_is_protected_when_backup_is_claimed():
    user, tenant, project, target, deployment = _workspace(
        spec={"kubernetes": {"directory": "/var/lib/etcd/backups"}}
    )

    # The backup is enqueued before the rollback exists, so the frozen payload
    # knows nothing about the snapshot yet.
    backup, created, reason = queue_backup(deployment)
    assert reason == ""
    assert created
    assert "protect" not in backup.payload
    enqueued_directories = backup.payload.get("directories")
    assert enqueued_directories

    rollback = _rollback(
        deployment,
        status=AgentCommand.Status.QUEUED,
        approved_at=NOW - timedelta(minutes=5),
        key="rb-after-enqueue",
    )

    client = APIClient()
    response = client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(target.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {NODE_TOKEN}",
    )

    assert response.status_code == 200
    assert response.data["command"]["id"] == str(backup.id)
    payload = response.data["command"]["payload"]
    # The fresh protect set reaches the agent with the rollback's snapshot, and
    # the directory candidate set from defect (a) is still attached untouched.
    assert list(payload.get("protect", [])) == [SNAPSHOT_NAME]
    assert payload.get("directories") == enqueued_directories

    backup.refresh_from_db()
    assert list(backup.payload.get("protect", [])) == [SNAPSHOT_NAME]
    assert backup.payload.get("directories") == enqueued_directories

    rollback.refresh_from_db()
    assert rollback.status == AgentCommand.Status.QUEUED
    assert protected_names(deployment) == [SNAPSHOT_NAME]


@pytest.mark.django_db
def test_expired_rollback_does_not_protect_its_snapshot():
    user, tenant, project, target, deployment = _workspace()
    _rollback(
        deployment,
        status=AgentCommand.Status.EXPIRED,
        approved_at=NOW - timedelta(days=2),
        key="rb-expired-directly",
    )
    assert protected_names(deployment) == []
