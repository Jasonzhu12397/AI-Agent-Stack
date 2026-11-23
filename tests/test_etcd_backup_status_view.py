"""The platform answer to "when did this cluster last back up?"."""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
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
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from packages.agent_contracts import etcd_backup as contract

URL = "/api/v1/cluster-deployments/{}/etcd-backup/"


def _workspace():
    user = get_user_model().objects.create_user(username="status-admin", password="x")
    tenant = Tenant.objects.create(name="Status tenant", slug="status-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="Status project", slug="status-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-status",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-status",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="t" * 64,
    )
    deployment = ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=eph,
        target_node=target,
        requested_by=user,
        cluster_name="lab",
        kubernetes_version="v1.37.2",
        idempotency_key="status-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec={},
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, target, deployment, client


def _command(tenant, project, target, user, deployment, *, names, when, status="succeeded"):
    hours_ago = (timezone.now() - when).total_seconds() / 3600
    result = {
        "backup": {"name": names[0], "verified": True} if names else None,
        "kept": names,
        "count": len(names),
        "failure_code": None,
        "cluster_name": "lab",
    }
    return AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=status,
        idempotency_key=f"run-{hours_ago:.2f}",
        plan_hash="0" * 64,
        result=result,
        deployment=deployment,
        finished_at=when,
    )


@pytest.mark.django_db
def test_a_cluster_without_a_backup_says_so():
    _, _, _, _, deployment, client = _workspace()

    response = client.get(URL.format(deployment.id))

    assert response.status_code == 200
    body = response.data
    assert body["ok"] is False
    assert body["stale"] is True
    assert body["last_run"] is None
    assert body["policy"]["valid"] is True
    assert body["policy"]["keep_count"] == contract.MIN_KEEP_COUNT
    assert body["policy"]["pre_upgrade_keep_max_age_hours"] == 168
    assert body["findings"][0]["code"] == "ETCD_BACKUP_NOT_RUN_YET"


@pytest.mark.django_db
def test_a_fresh_backup_is_not_stale_and_the_policy_comes_from_the_spec():
    user, tenant, project, target, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup_keep_count": 12}}
    deployment.save(update_fields=["spec"])
    now = datetime.datetime.now(datetime.UTC)
    name = contract.backup_filename("lab", now=now - datetime.timedelta(hours=2))
    _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[name],
        when=now - datetime.timedelta(hours=2),
    )

    response = client.get(URL.format(deployment.id))

    body = response.data
    assert body["policy"]["keep_count"] == 12
    assert body["newest"]["name"] == name
    assert 1.5 < body["newest_age_hours"] < 2.5
    assert body["stale"] is False
    assert body["ok"] is True
    assert body["prunable"] == []


@pytest.mark.django_db
def test_an_old_backup_is_reported_stale_and_still_usable():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    name = contract.backup_filename("lab", now=now - datetime.timedelta(hours=50))
    _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[name],
        when=now - datetime.timedelta(hours=50),
    )

    body = client.get(URL.format(deployment.id)).data

    assert body["stale"] is True
    assert body["newest"]["name"] == name
    assert any(item["code"] == "ETCD_BACKUP_STALE" for item in body["findings"])


@pytest.mark.django_db
def test_an_invalid_policy_is_reported_before_any_backup_runs():
    _, _, _, _, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup_keep_count": 1}}
    deployment.save(update_fields=["spec"])

    body = client.get(URL.format(deployment.id)).data

    assert body["policy"]["valid"] is False
    assert body["policy"]["error"]["code"] == "ETCD_BACKUP_RETENTION_INVALID"
    assert body["findings"][0]["code"] == "ETCD_BACKUP_RETENTION_INVALID"


@pytest.mark.django_db
def test_a_stranger_cannot_see_the_deployment_at_all():
    _, tenant, project, _, deployment, _ = _workspace()
    outsider = get_user_model().objects.create_user(username="outsider", password="x")
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.get(URL.format(deployment.id))

    assert response.status_code == 404


@pytest.mark.django_db
def test_the_view_shows_which_snapshots_a_rollback_is_holding():
    user, tenant, project, target, deployment, client = _workspace()
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.rollback",
        payload={"snapshot_path": "/var/lib/etcd/backups/" + snapshot},
        status=AgentCommand.Status.RUNNING,
        idempotency_key="view-protect",
        plan_hash="0" * 64,
        deployment=deployment,
    )

    body = client.get(URL.format(deployment.id)).data

    assert body["policy"]["protect"] == [snapshot]


POLICY_URL = "/api/v1/cluster-deployments/{}/etcd-backup/policy/"


@pytest.mark.django_db
def test_the_policy_endpoint_writes_a_validated_policy_into_the_spec():
    _, _, _, _, deployment, client = _workspace()

    response = client.post(
        POLICY_URL.format(deployment.id),
        {"keep_count": 12, "pre_upgrade_keep_max_age_hours": 336, "enabled": True},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["policy"]["keep_count"] == 12
    assert response.data["policy"]["pre_upgrade_keep_max_age_hours"] == 336
    deployment.refresh_from_db()
    assert deployment.spec["kubernetes"]["etcd_backup_keep_count"] == 12
    assert deployment.spec["kubernetes"]["etcd_backup_pre_upgrade_keep_max_age_hours"] == 336
    assert deployment.spec["kubernetes"]["etcd_backup"] is True
    from core.models import AuditEvent

    assert AuditEvent.objects.filter(
        event_type="cluster.etcd_backup.policy.updated"
    ).count() == 1


@pytest.mark.django_db
def test_the_policy_endpoint_refuses_a_policy_the_contract_refuses():
    _, _, _, _, deployment, client = _workspace()

    response = client.post(
        POLICY_URL.format(deployment.id), {"keep_count": 1}, format="json"
    )

    assert response.status_code == 400
    assert response.data["code"] == "ETCD_BACKUP_RETENTION_INVALID"
    deployment.refresh_from_db()
    assert deployment.spec == {}


@pytest.mark.django_db
def test_the_policy_endpoint_refuses_fields_it_does_not_own():
    _, _, _, _, deployment, client = _workspace()

    response = client.post(
        POLICY_URL.format(deployment.id),
        {"keep_count": 12, "cluster_name": "other"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "policy_fields_only"
    deployment.refresh_from_db()
    assert deployment.spec == {}


@pytest.mark.django_db
def test_the_policy_endpoint_can_switch_backups_off_without_a_contract_error():
    _, _, _, _, deployment, client = _workspace()

    response = client.post(POLICY_URL.format(deployment.id), {"enabled": False}, format="json")

    assert response.status_code == 200
    assert response.data["policy"]["enabled"] is False
    deployment.refresh_from_db()
    assert deployment.spec["kubernetes"]["etcd_backup"] is False


@pytest.mark.django_db
def test_the_policy_endpoint_needs_a_body():
    _, _, _, _, deployment, client = _workspace()

    response = client.post(POLICY_URL.format(deployment.id), {}, format="json")

    assert response.status_code == 400
    assert response.data["code"] == "policy_empty"


@pytest.mark.django_db
def test_the_view_reports_the_disk_floor_the_policy_sets():
    _, _, _, _, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup_min_free_bytes": 1073741824}}
    deployment.save(update_fields=["spec"])

    body = client.get(URL.format(deployment.id)).data

    assert body["policy"]["min_free_bytes"] == 1073741824


@pytest.mark.django_db
def test_the_restore_point_is_the_pre_upgrade_snapshot_even_when_a_backup_is_newer():
    user, tenant, project, target, deployment, client = _workspace()
    now = timezone.now()
    pre = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    periodic = contract.backup_filename("lab", now=now)
    # The recurring backup ran later, but undoing the upgrade means putting the
    # pre-upgrade file back: newer is not the same as the state before the change.
    _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[periodic, pre],
        when=now,
    )
    body = client.get(URL.format(deployment.id)).data

    assert body["newest"]["name"] == periodic
    point = body["restore_point"]
    assert point["name"] == pre
    assert point["kind"] == "pre_upgrade"
    assert point["listed"] is True
    assert point["version"] == "v1.37.2"
    assert point["path"] == "/var/lib/etcd/backups/" + pre
    assert point["age_hours"] >= 0


@pytest.mark.django_db
def test_a_cluster_that_only_ever_backed_up_can_still_be_rolled_back_to_the_newest():
    user, tenant, project, target, deployment, client = _workspace()
    periodic = contract.backup_filename("lab", now=timezone.now())
    _command(
        tenant, project, target, user, deployment, names=[periodic], when=timezone.now()
    )

    point = client.get(URL.format(deployment.id)).data["restore_point"]

    assert point["name"] == periodic
    assert point["kind"] == "periodic"


@pytest.mark.django_db
def test_an_upgrade_reports_a_restore_point_before_the_next_backup_lists_the_directory():
    user, tenant, project, target, deployment, client = _workspace()
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.upgrade",
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="upgrade-restore-point",
        plan_hash="0" * 64,
        deployment=deployment,
        result={
            "etcd_snapshot": {
                "path": "/var/lib/etcd/backups/" + snapshot,
                "verified": True,
                "taken": True,
            }
        },
    )

    point = client.get(URL.format(deployment.id)).data["restore_point"]

    assert point["name"] == snapshot
    assert point["path"] == "/var/lib/etcd/backups/" + snapshot
    assert point["listed"] is False


@pytest.mark.django_db
def test_an_unverified_or_unfinished_upgrade_offers_no_restore_point():
    user, tenant, project, target, deployment, client = _workspace()
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    for key, status, verified in (
        ("upgrade-failed", AgentCommand.Status.FAILED, True),
        ("upgrade-unverified", AgentCommand.Status.SUCCEEDED, False),
    ):
        AgentCommand.objects.create(
            tenant=tenant,
            project=project,
            node=target,
            requested_by=user,
            command_type="cluster.kubernetes.version.upgrade",
            payload={"cluster_name": "lab"},
            status=status,
            idempotency_key=key,
            plan_hash="0" * 64,
            deployment=deployment,
            result={
                "etcd_snapshot": {
                    "path": "/var/lib/etcd/backups/" + snapshot,
                    "verified": verified,
                    "taken": True,
                }
            },
        )

    body = client.get(URL.format(deployment.id)).data

    assert body["restore_point"] is None


RUN_URL = "/api/v1/cluster-deployments/{}/etcd-backup/run/"


@pytest.mark.django_db
def test_an_operator_can_queue_a_backup_now_without_an_approval():
    _, tenant, project, target, deployment, client = _workspace()

    response = client.post(RUN_URL.format(deployment.id))

    assert response.status_code == 202
    assert response.data["created"] is True
    assert response.data["status"] == AgentCommand.Status.QUEUED
    command = AgentCommand.objects.get(pk=response.data["command_id"])
    assert command.command_type == contract.ACTION
    assert command.node_id == target.id
    assert command.payload["cluster_name"] == "lab"
    assert command.status == AgentCommand.Status.QUEUED


@pytest.mark.django_db
def test_pressing_back_up_now_twice_in_one_hour_takes_one_snapshot():
    _, tenant, project, _, deployment, client = _workspace()

    first = client.post(RUN_URL.format(deployment.id)).data
    second = client.post(RUN_URL.format(deployment.id)).data

    assert second["command_id"] == first["command_id"]
    assert second["created"] is False
    assert second["code"] == "ETCD_BACKUP_ALREADY_QUEUED"
    assert AgentCommand.objects.filter(command_type=contract.ACTION).count() == 1


@pytest.mark.django_db
def test_an_operator_run_carries_the_same_policy_and_protection_the_scheduler_would():
    user, tenant, project, target, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup_keep_count": 12}}
    deployment.save(update_fields=["spec"])
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.rollback",
        payload={"snapshot_path": "/var/lib/etcd/backups/" + snapshot},
        status=AgentCommand.Status.RUNNING,
        idempotency_key="protect-on-manual-run",
        plan_hash="0" * 64,
        deployment=deployment,
    )

    command = AgentCommand.objects.get(
        pk=client.post(RUN_URL.format(deployment.id)).data["command_id"]
    )

    assert command.payload["keep_count"] == 12
    assert command.payload["protect"] == [snapshot]


@pytest.mark.django_db
def test_a_cluster_with_backups_switched_off_cannot_be_backed_up_on_demand():
    _, _, _, _, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup": False}}
    deployment.save(update_fields=["spec"])

    response = client.post(RUN_URL.format(deployment.id))

    assert response.status_code == 409
    assert response.data["code"] == "ETCD_BACKUP_DISABLED"
    assert AgentCommand.objects.count() == 0


@pytest.mark.django_db
def test_a_cluster_without_a_target_agent_cannot_be_backed_up_on_demand():
    _, _, _, _, deployment, client = _workspace()
    deployment.target_node = None
    deployment.save(update_fields=["target_node"])

    response = client.post(RUN_URL.format(deployment.id))

    assert response.status_code == 409
    assert response.data["code"] == "ETCD_BACKUP_NO_TARGET_AGENT"


@pytest.mark.django_db
def test_a_stranger_cannot_queue_a_backup():
    _, _, _, _, deployment, _ = _workspace()
    stranger = get_user_model().objects.create_user(username="status-stranger", password="x")
    client = APIClient()
    client.force_authenticate(user=stranger)

    assert client.post(RUN_URL.format(deployment.id)).status_code == 404


CHECK_URL = "/api/v1/cluster-deployments/{}/etcd-backup/check/"


@pytest.mark.django_db
def test_an_operator_can_ask_the_cluster_what_is_on_disk_without_writing():
    _, tenant, project, target, deployment, client = _workspace()

    response = client.post(CHECK_URL.format(deployment.id))

    assert response.status_code == 202
    command = AgentCommand.objects.get(pk=response.data["command_id"])
    assert command.command_type == contract.STATUS_ACTION
    assert command.node_id == target.id
    assert command.status == AgentCommand.Status.QUEUED


@pytest.mark.django_db
def test_two_checks_inside_one_bucket_ask_the_cluster_once():
    _, _, _, _, deployment, client = _workspace()

    first = client.post(CHECK_URL.format(deployment.id)).data
    second = client.post(CHECK_URL.format(deployment.id)).data

    assert second["command_id"] == first["command_id"]
    assert second["code"] == "ETCD_BACKUP_CHECK_ALREADY_QUEUED"
    assert AgentCommand.objects.count() == 1


@pytest.mark.django_db
def test_a_check_reports_the_live_directory_even_between_backup_runs():
    _, tenant, project, target, deployment, client = _workspace()
    now = timezone.now()
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    periodic = contract.backup_filename("lab", now=now)
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=deployment.requested_by,
        command_type=contract.STATUS_ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="status-report",
        plan_hash="0" * 64,
        result={"entries": [{"name": periodic}, {"name": snapshot}]},
        deployment=deployment,
        finished_at=now,
    )

    body = client.get(URL.format(deployment.id)).data

    assert body["reported_by"] == contract.STATUS_ACTION
    assert body["newest"]["name"] == periodic
    assert body["restore_point"]["name"] == snapshot
    assert body["restore_point"]["listed"] is True


@pytest.mark.django_db
def test_a_failed_newest_run_is_not_hidden_behind_an_older_good_one():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    good = contract.backup_filename("lab", now=now - datetime.timedelta(hours=20))
    _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[good],
        when=now - datetime.timedelta(hours=20),
    )
    failed = _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[],
        when=now - datetime.timedelta(minutes=5),
        status="failed",
    )
    failed.result = {
        "ok": False,
        "failure_code": "ETCD_BACKUP_DISK_LOW",
        "detail": "need 512 MiB, 130 MiB free",
    }
    failed.save(update_fields=["result"])

    response = client.get(URL.format(deployment.id))

    body = response.data
    # The disk still holds yesterday's snapshot, so newest/age are readable...
    assert body["newest"]["name"] == good
    # ...but the cluster is not healthy: the newest attempt failed.
    assert body["ok"] is False
    assert body["last_run_failed"] == "ETCD_BACKUP_DISK_LOW"
    assert any(
        finding["code"] == "ETCD_BACKUP_DISK_LOW" for finding in body["findings"]
    )


@pytest.mark.django_db
def test_a_command_nobody_picked_up_is_not_a_quiet_cluster():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    good = contract.backup_filename("lab", now=now - datetime.timedelta(hours=20))
    earlier = _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[good],
        when=now - datetime.timedelta(hours=20),
    )
    AgentCommand.objects.filter(pk=earlier.pk).update(
        created_at=now - datetime.timedelta(hours=20)
    )
    stuck = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.QUEUED,
        idempotency_key="run-stuck",
        plan_hash="1" * 64,
        deployment=deployment,
    )
    AgentCommand.objects.filter(pk=stuck.pk).update(
        created_at=now - datetime.timedelta(hours=9)
    )

    response = client.get(URL.format(deployment.id))

    body = response.data
    assert body["ok"] is False
    assert 8.5 < body["run_stuck_hours"] < 9.5
    assert any(
        finding["code"] == "ETCD_BACKUP_RUN_STUCK" for finding in body["findings"]
    )


@pytest.mark.django_db
def test_a_run_that_is_merely_in_flight_is_left_alone():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    good = contract.backup_filename("lab", now=now - datetime.timedelta(hours=1))
    _command(
        tenant,
        project,
        target,
        user,
        deployment,
        names=[good],
        when=now - datetime.timedelta(hours=1),
    )
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.RUNNING,
        idempotency_key="run-flying",
        plan_hash="2" * 64,
        deployment=deployment,
    )

    response = client.get(URL.format(deployment.id))

    body = response.data
    assert body["ok"] is True
    assert "run_stuck_hours" not in body


@pytest.mark.django_db
def test_a_stuck_command_beyond_the_history_window_is_still_found():
    user, tenant, project, target, deployment, _ = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    stuck = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.QUEUED,
        idempotency_key="run-ancient-stuck",
        plan_hash="4" * 64,
        deployment=deployment,
    )
    AgentCommand.objects.filter(pk=stuck.pk).update(
        created_at=now - datetime.timedelta(hours=20)
    )
    for index in range(14):
        _command(
            tenant,
            project,
            target,
            user,
            deployment,
            names=[contract.backup_filename("lab", now=now - datetime.timedelta(hours=index + 1))],
            when=now - datetime.timedelta(hours=index + 1),
        )

    state = etcd_backup_state(deployment, now=now)

    assert state["run_stuck_hours"] is not None
    assert 19 < state["run_stuck_hours"] < 21
    assert any(finding["code"] == "ETCD_BACKUP_RUN_STUCK" for finding in state["findings"])


@pytest.mark.django_db
def test_a_failed_command_does_not_block_a_retry_in_the_same_hour():
    user, tenant, project, target, deployment, client = _workspace()
    dead = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.FAILED,
        idempotency_key=f"etcd-backup:{deployment.id}:20261123T02",
        plan_hash="5" * 64,
        deployment=deployment,
    )
    # Freeze the bucket so the key the view builds is the one above.
    url = f"/api/v1/cluster-deployments/{deployment.id}/etcd-backup/run/"
    with patch("core.etcd_backup_queue.hour_bucket", return_value="20261123T02"):
        first = client.post(url)
        second = client.post(url)

    assert first.status_code == 202
    assert first.data["created"] is True
    assert first.data["code"] == ""
    assert first.data["command_id"] != str(dead.id)
    # The retry itself is idempotent: a double click returns the same command.
    assert second.data["created"] is False
    assert second.data["command_id"] == first.data["command_id"]
    assert (
        AgentCommand.objects.filter(deployment=deployment, status="queued").count() == 1
    )


@pytest.mark.django_db
def test_a_live_command_in_the_hour_still_wins_over_a_retry():
    user, tenant, project, target, deployment, client = _workspace()
    live = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"etcd-backup:{deployment.id}:20261123T02",
        plan_hash="6" * 64,
        deployment=deployment,
    )

    url = f"/api/v1/cluster-deployments/{deployment.id}/etcd-backup/run/"
    with patch("core.etcd_backup_queue.hour_bucket", return_value="20261123T02"):
        response = client.post(url)

    assert response.data["created"] is False
    assert response.data["command_id"] == str(live.id)
    assert response.data["code"] == "ETCD_BACKUP_ALREADY_QUEUED"


@pytest.mark.django_db
def test_a_cluster_switched_off_says_so_instead_of_never_backed_up():
    _, _, _, _, deployment, client = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup": False}}
    deployment.save(update_fields=["spec"])

    response = client.get(URL.format(deployment.id))

    body = response.data
    assert body["enabled"] is False
    assert body["ok"] is False
    assert body["findings"] == []
    assert "not_run_yet" not in body


@pytest.mark.django_db
def test_a_reported_snapshot_path_is_not_rewritten_from_the_policy_directory():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    name = "snapshot-pre-lab-v1.38.0-20261123T010000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.upgrade",
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="upgrade-snapshot",
        plan_hash="7" * 64,
        deployment=deployment,
        finished_at=now,
        result={
            "etcd_snapshot": {
                "path": f"/var/lib/etcd/backups-labcache/{name}",
                "verified": True,
                "taken": True,
            }
        },
    )

    body = client.get(URL.format(deployment.id)).data

    point = body["restore_point"]
    assert point["name"] == name
    # The snapshot lives in the directory the upgrade was told to use, not in
    # whatever the policy says today.
    assert point["path"] == f"/var/lib/etcd/backups-labcache/{name}"
    assert point["listed"] is False


@pytest.mark.django_db
def test_a_restore_point_from_a_listing_uses_the_directory_that_was_listed():
    user, tenant, project, target, deployment, client = _workspace()
    now = datetime.datetime.now(datetime.UTC)
    name = "snapshot-pre-lab-v1.38.0-20261123T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.STATUS_ACTION,
        payload={
            "cluster_name": "lab",
            "directory": "/var/lib/etcd/backups-old",
        },
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="status-old-directory",
        plan_hash="8" * 64,
        deployment=deployment,
        finished_at=now,
        result={"entries": [name]},
    )

    point = client.get(URL.format(deployment.id)).data["restore_point"]

    assert point["listed"] is True
    assert point["path"] == f"/var/lib/etcd/backups-old/{name}"
