"""Slot identity for queued etcd backups.

A backup slot answers two questions at once: why this backup was queued, and
which mutation it describes. The scheduler's hourly slot and the reactive
change slot are different reasons and must coexist in the same hour; a change
slot is bound to the mutation command that triggered it, so a repeated delivery
of that command replays one slot while two distinct mutations keep two slots.
"""

from __future__ import annotations

import datetime

import pytest
from core.models import (
    AgentCommand,
    AgentNode,
    ClusterDeployment,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model

ACTION = "cluster.kubernetes.backup.run"
MOMENT = datetime.datetime(2026, 11, 23, 3, 10, tzinfo=datetime.UTC)
BUCKET = "20261123T03"


def _workspace():
    user = get_user_model().objects.create_user(username="slot-admin", password="x")
    tenant = Tenant.objects.create(name="Slot tenant", slug="slot-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="Slot project", slug="slot-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-slot",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-slot",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="t" * 64,
    )
    return user, tenant, project, eph, target


def _deployment(tenant, project, eph, target, user, key):
    return ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=eph,
        target_node=target,
        requested_by=user,
        cluster_name="lab",
        kubernetes_version="v1.37.2",
        idempotency_key=f"slot-{key}",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec={},
    )


def _change_command(tenant, project, target, user, deployment, key):
    return AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.upgrade",
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        result={"upgrade_performed": True},
        plan_hash="a" * 64,
        deployment=deployment,
        idempotency_key=f"change-{key}",
    )


def _hourly_key(deployment) -> str:
    return f"etcd-backup:{deployment.id}:{BUCKET}"


def _change_key(deployment, command: AgentCommand) -> str:
    return f"etcd-backup:{deployment.id}:{BUCKET}:change:cmd:{command.id}"


@pytest.mark.django_db
def test_hourly_and_change_slots_coexist_in_the_same_hour():
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "coexist")
    change = _change_command(tenant, project, target, user, deployment, "coexist")

    hourly, hourly_created, _ = queue_backup(deployment, now=MOMENT)
    after, change_created, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )

    assert hourly is not None and after is not None
    assert hourly_created is True
    assert change_created is True
    assert after.id != hourly.id
    assert hourly.idempotency_key == _hourly_key(deployment)
    assert after.idempotency_key == _change_key(deployment, change)
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 2


@pytest.mark.django_db
def test_two_distinct_changes_in_one_hour_keep_distinct_slots():
    # Two real mutations in the same hour describe two different post-states.
    # Merging them would hand back the snapshot that predates the second one,
    # so each source command owns its own slot. They share the same retention
    # policy; the hourly slot itself stays one per hour.
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "two-changes")
    first = _change_command(tenant, project, target, user, deployment, "one")
    second = _change_command(tenant, project, target, user, deployment, "two")

    first_backup, first_created, _ = queue_backup(
        deployment, now=MOMENT, change_source=first
    )
    second_backup, second_created, _ = queue_backup(
        deployment, now=MOMENT, change_source=second
    )

    assert first_created is True
    assert second_created is True
    assert first_backup.id != second_backup.id
    assert first_backup.idempotency_key == _change_key(deployment, first)
    assert second_backup.idempotency_key == _change_key(deployment, second)
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 2


@pytest.mark.django_db
def test_the_same_change_replayed_queues_one_slot_only():
    # The agent may deliver the same settled command more than once (result
    # replay). That delivery is the same mutation, so the backup slot is reused.
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "replay")
    change = _change_command(tenant, project, target, user, deployment, "replay")

    first, created_first, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )
    second, created_second, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 1


@pytest.mark.django_db
def test_change_slot_retry_chain_keeps_the_source_bound_key():
    # A failed change slot must not answer "already queued" to the replay that
    # fixes it. The retry chain hangs off the same source-bound key with the
    # existing ":retry:N" suffix; the prefix match must still find it.
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "retry-change")
    change = _change_command(tenant, project, target, user, deployment, "retry")

    dead, created, _ = queue_backup(deployment, now=MOMENT, change_source=change)
    assert created is True
    dead.status = AgentCommand.Status.FAILED
    dead.save(update_fields=["status"])

    retry, retry_created, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )
    assert retry_created is True
    assert retry.id != dead.id
    assert retry.idempotency_key == f"{_change_key(deployment, change)}:retry:1"

    again, again_created, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )
    assert again_created is False
    assert again.id == retry.id


@pytest.mark.django_db
def test_hourly_slot_retry_chain_is_unchanged():
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "retry-hourly")

    dead, created, _ = queue_backup(deployment, now=MOMENT)
    assert created is True
    dead.status = AgentCommand.Status.FAILED
    dead.save(update_fields=["status"])

    retry, retry_created, _ = queue_backup(deployment, now=MOMENT)
    assert retry_created is True
    assert retry.id != dead.id
    assert retry.idempotency_key == f"{_hourly_key(deployment)}:retry:1"

    again, again_created, _ = queue_backup(deployment, now=MOMENT)
    assert again_created is False
    assert again.id == retry.id


@pytest.mark.django_db
def test_legacy_hourly_row_is_reused_without_a_ghost_duplicate():
    # Deployments already carry commands keyed by the old hourly scheme
    # (no suffix). The hourly lookup must still match that scheme, so an
    # existing current-hour snapshot is reused instead of being copied.
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "legacy-hourly")
    legacy = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=_hourly_key(deployment),
        plan_hash="0" * 64,
        deployment=deployment,
    )

    found, created, _ = queue_backup(deployment, now=MOMENT)

    assert created is False
    assert found.id == legacy.id
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 1


@pytest.mark.django_db
def test_legacy_coarse_change_row_does_not_collide_with_source_slot():
    # The old scheme collapsed every change in an hour into one ":change" slot.
    # Those historical rows stay as records; a new mutation opens a source-bound
    # slot beside them, and nothing falls back to the coarse key to reuse them.
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "legacy-change")
    change = _change_command(tenant, project, target, user, deployment, "legacy")
    coarse_key = f"etcd-backup:{deployment.id}:{BUCKET}:change"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=coarse_key,
        plan_hash="0" * 64,
        deployment=deployment,
    )

    after, created, _ = queue_backup(
        deployment, now=MOMENT, change_source=change
    )

    assert created is True
    assert after.idempotency_key == _change_key(deployment, change)
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 2


@pytest.mark.django_db
def test_changes_in_different_hours_do_not_share_a_slot():
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, "hours")
    early = datetime.datetime(2026, 11, 23, 3, 10, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 11, 23, 4, 10, tzinfo=datetime.UTC)
    change = _change_command(tenant, project, target, user, deployment, "hours")

    first, first_created, _ = queue_backup(
        deployment, now=early, change_source=change
    )
    second, second_created, _ = queue_backup(
        deployment, now=late, change_source=change
    )

    assert first_created is True
    assert second_created is True
    assert second.id != first.id
