"""The scheduler that turns "backups every hour" into queued commands."""

from __future__ import annotations

import datetime
import json
from io import StringIO

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
from django.core.management import call_command

ACTION = "cluster.kubernetes.backup.run"


def _workspace():
    user = get_user_model().objects.create_user(username="backup-admin", password="x")
    tenant = Tenant.objects.create(name="Backup tenant", slug="backup-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="Backup project", slug="backup-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-1",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-1",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="t" * 64,
    )
    return user, tenant, project, eph, target


def _deployment(tenant, project, eph, target, user, **overrides):
    return ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=eph,
        target_node=target,
        requested_by=user,
        cluster_name="lab",
        kubernetes_version="v1.37.2",
        idempotency_key="backup-" + str(overrides.pop("key", "1")),
        status=overrides.pop("status", ClusterDeployment.Status.SUCCEEDED),
        spec=overrides.pop("spec", {}),
    )


def _run(**options):
    stdout = StringIO()
    call_command("queue_etcd_backups", stdout=stdout, **options)
    return json.loads(stdout.getvalue())


@pytest.mark.django_db
def test_an_installed_cluster_gets_one_backup_per_hour():
    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)

    first = _run()
    assert [item["deployment_id"] for item in first["queued"]] == [str(deployment.id)]
    commands = AgentCommand.objects.filter(command_type=ACTION)
    assert commands.count() == 1
    command = commands.get()
    assert command.node_id == target.id
    assert command.status == AgentCommand.Status.QUEUED
    assert command.payload["execution_mode"] == "execute"
    assert command.payload["confirm_cluster_name"] == "lab"
    assert command.deployment_id == deployment.id

    second = _run()
    assert second["queued"][0]["created"] is False
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 1


@pytest.mark.django_db
def test_dry_run_reports_without_queueing():
    user, tenant, project, eph, target = _workspace()
    _deployment(tenant, project, eph, target, user)

    report = _run(dry_run=True)

    assert report["dry_run"] is True
    assert report["queued"][0]["command_id"] is None
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 0


@pytest.mark.django_db
def test_a_disabled_or_agentless_cluster_is_skipped_with_a_reason():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        key="disabled",
        spec={"kubernetes": {"etcd_backup": False}},
    )
    _deployment(tenant, project, eph, None, user, key="agentless")

    report = _run()

    assert report["queued"] == []
    reasons = {item["reason"] for item in report["skipped"]}
    assert reasons == {"disabled", "no_target_agent"}


@pytest.mark.django_db
def test_a_cluster_that_has_not_succeeded_yet_is_left_alone():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        key="midway",
        status=ClusterDeployment.Status.PROVISIONING,
    )

    report = _run()

    assert report["queued"] == []
    assert AgentCommand.objects.filter(command_type=ACTION).count() == 0


@pytest.mark.django_db
def test_retention_overrides_reach_the_payload():
    user, tenant, project, eph, target = _workspace()
    _deployment(tenant, project, eph, target, user)

    _run(keep_count=12, keep_max_age_hours=72, directory="/var/lib/etcd/backups/lab")

    command = AgentCommand.objects.filter(command_type=ACTION).get()
    assert command.payload["keep_count"] == 12
    assert command.payload["keep_max_age_hours"] == 72
    assert command.payload["directory"] == "/var/lib/etcd/backups/lab"


def test_both_compose_files_ship_the_scheduler():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    for relative in ("deploy/docker-compose.yml", "deploy/docker-compose.cloud.yml"):
        document = yaml.safe_load((root / relative).read_text())
        service = document["services"]["etcd-backup-scheduler"]
        command = service["command"]
        assert command[0] == "sh"
        assert "queue_etcd_backups" in command[-1]
        assert "ETCD_BACKUP_INTERVAL_SECONDS" in command[-1]
        assert service["security_opt"] == ["no-new-privileges:true"]


@pytest.mark.django_db
def test_the_policy_a_deployment_declares_reaches_the_command():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        spec={
            "kubernetes": {
                "etcd_backup_keep_count": 24,
                "etcd_backup_keep_max_age_hours": 96,
                "etcd_backup_pre_upgrade_keep_max_age_hours": 336,
                "etcd_backup_directory": "/var/lib/etcd/backups/lab",
            }
        },
    )

    _run()

    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert payload["keep_count"] == 24
    assert payload["keep_max_age_hours"] == 96
    assert payload["pre_upgrade_keep_max_age_hours"] == 336
    assert payload["directory"] == "/var/lib/etcd/backups/lab"


@pytest.mark.django_db
def test_a_command_line_override_wins_over_the_spec():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        spec={"kubernetes": {"etcd_backup_keep_count": 24}},
    )

    _run(keep_count=8)

    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert payload["keep_count"] == 8


@pytest.mark.django_db
def test_a_spec_policy_the_contract_refuses_is_reported_instead_of_queued():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        spec={"kubernetes": {"etcd_backup_keep_count": 1}},
    )

    report = _run()

    assert report["queued"] == []
    assert report["skipped"][0]["reason"] == "ETCD_BACKUP_RETENTION_INVALID"


@pytest.mark.django_db
def test_a_rollback_still_to_be_run_protects_its_snapshot_from_pruning():
    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.rollback",
        payload={"cluster_name": "lab", "snapshot_path": "/var/lib/etcd/backups/" + snapshot},
        status=AgentCommand.Status.QUEUED,
        idempotency_key="rollback-open",
        plan_hash="0" * 64,
        deployment=deployment,
    )

    _run()

    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert payload["protect"] == [snapshot]


@pytest.mark.django_db
def test_a_failed_rollback_stops_holding_its_snapshot():
    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.rollback",
        payload={
            "snapshot_path": (
                "/var/lib/etcd/backups/snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
            )
        },
        status=AgentCommand.Status.FAILED,
        idempotency_key="rollback-failed",
        plan_hash="0" * 64,
        deployment=deployment,
    )

    _run()

    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert "protect" not in payload


@pytest.mark.django_db
def test_a_declared_disk_floor_reaches_the_command():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        spec={"kubernetes": {"etcd_backup_min_free_bytes": 2 * 1024 * 1024 * 1024}},
    )

    _run()

    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert payload["min_free_bytes"] == 2 * 1024 * 1024 * 1024


def test_both_compose_files_ship_the_watchdog_and_only_defined_dependencies():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    for relative in ("deploy/docker-compose.yml", "deploy/docker-compose.cloud.yml"):
        document = yaml.safe_load((root / relative).read_text())
        services = document["services"]
        service = services["etcd-backup-watchdog"]
        assert "check_etcd_backups" in service["command"][-1]
        assert service["security_opt"] == ["no-new-privileges:true"]
        # A depends_on that names a service this file does not define makes the
        # whole project invalid: docker compose refuses to start anything.
        for dependency in service.get("depends_on") or {}:
            assert dependency in services, f"{relative}: undefined {dependency}"
        for other_name, other in services.items():
            for dependency in other.get("depends_on") or {}:
                assert dependency in services, f"{relative}: {other_name} -> {dependency}"


@pytest.mark.django_db
def test_an_override_of_zero_survives_because_zero_is_a_policy():
    from core.etcd_backup_queue import backup_payload

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)

    payload = backup_payload(deployment, {"min_free_bytes": 0})

    assert payload["min_free_bytes"] == 0
    # An override that was never given must still leave the default alone.
    assert backup_payload(deployment)["min_free_bytes"] > 0


@pytest.mark.django_db
def test_an_explicit_zero_floor_reaches_the_scheduled_command():
    user, tenant, project, eph, target = _workspace()
    _deployment(tenant, project, eph, target, user)

    _run(min_free_bytes=0)

    command = AgentCommand.objects.get(command_type=ACTION)
    assert command.payload["min_free_bytes"] == 0


@pytest.mark.django_db
def test_an_unset_flag_leaves_the_spec_policy_untouched():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        spec={
            "kubernetes": {
                "etcd_backup_keep_count": 12,
                "etcd_backup_min_free_bytes": 1073741824,
            }
        },
    )

    _run()

    payload = AgentCommand.objects.get(command_type=ACTION).payload
    assert payload["keep_count"] == 12
    assert payload["min_free_bytes"] == 1073741824


@pytest.mark.django_db
def test_a_succeeded_rollback_releases_the_snapshot_it_restored_from():
    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.rollback",
        payload={
            "snapshot_path": (
                "/var/lib/etcd/backups/snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
            )
        },
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="rollback-done",
        plan_hash="0" * 64,
        deployment=deployment,
    )

    _run()

    # The file did its job. Keeping it protected forever is how a backup
    # directory never shrinks.
    payload = AgentCommand.objects.filter(command_type=ACTION).get().payload
    assert "protect" not in payload


@pytest.mark.django_db
def test_a_hand_edited_switch_is_skipped_with_its_own_code():
    user, tenant, project, eph, target = _workspace()
    _deployment(
        tenant, project, eph, target, user, spec={"kubernetes": {"etcd_backup": 0}}
    )

    report = _run()

    assert report["queued"] == []
    assert report["skipped"][0]["reason"] == "ETCD_BACKUP_ENABLED_INVALID"
    assert not AgentCommand.objects.filter(command_type=ACTION).exists()


@pytest.mark.django_db
def test_a_backup_after_a_change_is_not_the_scheduled_one():
    from core.etcd_backup_queue import queue_backup

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user)
    moment = datetime.datetime(2026, 11, 23, 3, 10, tzinfo=datetime.UTC)
    scheduled, _, _ = queue_backup(deployment, now=moment)
    assert scheduled is not None

    # The scheduled command covers state from before the change: a change made
    # in the same hour needs its own snapshot, not that old one handed back.
    after, created, _ = queue_backup(deployment, now=moment, trigger="change")
    assert created is True
    assert after.id != scheduled.id
    assert after.idempotency_key.endswith(":change")

    again, created, _ = queue_backup(deployment, now=moment, trigger="change")
    assert created is False
    assert again.id == after.id
