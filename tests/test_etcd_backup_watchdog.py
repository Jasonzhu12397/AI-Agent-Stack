"""The watchdog: a missing or stale backup becomes an audit event and a failure."""

from __future__ import annotations

import datetime
import json
from io import StringIO

import pytest
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
from django.core.management import call_command
from django.core.management.base import CommandError

from packages.agent_contracts import etcd_backup as contract


def _workspace():
    user = get_user_model().objects.create_user(username="watchdog-admin", password="x")
    tenant = Tenant.objects.create(name="Watchdog tenant", slug="watchdog-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(tenant=tenant, name="W", slug="watchdog-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-w",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-w",
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
        idempotency_key="watchdog-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec={},
    )
    # The scheduler is alive in this workspace: without a heartbeat every
    # cluster would be reported as scheduler-silent, which is a different fact.
    _beat(tenant)
    return tenant, project, target, deployment, user


def _scheduler_events(tenant):
    return AuditEvent.objects.filter(
        tenant=tenant, event_type="cluster.etcd_backup.scheduler.run"
    )


def _beat(tenant, *, hours_ago: float = 0) -> None:
    from core.audit import record_audit_event

    record_audit_event(
        None,
        tenant,
        "cluster.etcd_backup.scheduler.run",
        "etcd_backup_scheduler",
        "",
        outcome=AuditEvent.Outcome.SUCCESS,
        metadata={"bucket": "test"},
    )
    if hours_ago:
        event = _scheduler_events(tenant).first()
        AuditEvent.objects.filter(pk=event.pk).update(
            created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours_ago)
        )


def _run(**options):
    stdout = StringIO()
    error = ""
    try:
        call_command("check_etcd_backups", stdout=stdout, **options)
    except CommandError as failure:
        error = str(failure)
    return json.loads(stdout.getvalue()), error


def _backup(tenant, project, target, deployment, user, *, hours_ago: float):
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours_ago)
    name = contract.backup_filename("lab", now=when)
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type=contract.ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"run-{hours_ago}",
        plan_hash="0" * 64,
        result={"backup": {"name": name}, "kept": [name]},
        deployment=deployment,
        finished_at=when,
    )
    return name


@pytest.mark.django_db
def test_a_cluster_that_never_backed_up_is_a_problem_and_an_audit_event():
    tenant, _, _, deployment, _ = _workspace()

    report, error = _run()

    assert report["problems"][0]["code"] == "ETCD_BACKUP_NOT_RUN_YET"
    assert deployment.cluster_name in error
    events = AuditEvent.objects.filter(event_type="cluster.etcd_backup.problem")
    assert events.count() == 1
    assert events.get().outcome == AuditEvent.Outcome.FAILURE
    assert events.get().metadata["code"] == "ETCD_BACKUP_NOT_RUN_YET"


@pytest.mark.django_db
def test_a_fresh_backup_is_quiet():
    tenant, project, target, deployment, user = _workspace()
    name = _backup(tenant, project, target, deployment, user, hours_ago=2)

    report, error = _run()

    assert error == ""
    assert report["problems"] == []
    assert report["clusters"][0]["ok"] is True
    assert report["clusters"][0]["newest"] == name
    assert AuditEvent.objects.filter(event_type="cluster.etcd_backup.problem").count() == 0


@pytest.mark.django_db
def test_an_old_backup_is_reported_stale_with_its_age():
    tenant, project, target, deployment, user = _workspace()
    _backup(tenant, project, target, deployment, user, hours_ago=50)

    report, error = _run()

    assert report["problems"][0]["code"] == "ETCD_BACKUP_STALE"
    assert 49 < report["problems"][0]["newest_age_hours"] < 51
    assert "ETCD_BACKUP_STALE" in error


@pytest.mark.django_db
def test_dry_run_reports_without_writing_audit_events():
    _workspace()

    report, error = _run(dry_run=True)

    assert report["dry_run"] is True
    assert report["problems"]
    assert error
    assert AuditEvent.objects.filter(event_type="cluster.etcd_backup.problem").count() == 0


@pytest.mark.django_db
def test_a_disabled_cluster_is_neither_a_problem_nor_an_audit_event():
    tenant, _, _, deployment, _ = _workspace()
    deployment.spec = {"kubernetes": {"etcd_backup": False}}
    deployment.save(update_fields=["spec"])

    report, error = _run()

    assert error == ""
    assert report["clusters"][0]["code"] == "ETCD_BACKUP_DISABLED"
    assert AuditEvent.objects.filter(event_type="cluster.etcd_backup.problem").count() == 0


@pytest.mark.django_db
def test_the_watchdog_reads_the_last_report_and_does_not_guess_beyond_it():
    tenant, project, target, deployment, user = _workspace()
    _backup(tenant, project, target, deployment, user, hours_ago=2)
    deployment.target_node = None
    deployment.save(update_fields=["target_node"])

    report, error = _run()

    # The newest report is still fresh, so the answer is "fresh": the watchdog
    # reports what the cluster said, and does not invent a state it cannot see.
    assert report["clusters"][0]["ok"] is True
    assert error == ""


def _events(deployment, event_type):
    return AuditEvent.objects.filter(
        tenant=deployment.tenant,
        event_type=event_type,
        resource_id=str(deployment.id),
    )


@pytest.mark.django_db
def test_a_problem_that_persists_is_audited_once_not_once_per_probe():
    _, _, _, deployment, _ = _workspace()

    for _ in range(3):
        _report, error = _run()
        assert error

    assert _events(deployment, "cluster.etcd_backup.problem").count() == 1


@pytest.mark.django_db
def test_a_changed_problem_is_audited_again_because_it_is_news():
    tenant, project, target, deployment, user = _workspace()
    _backup(tenant, project, target, deployment, user, hours_ago=100)
    _report, error = _run()
    assert error
    _events(deployment, "cluster.etcd_backup.problem").get()

    # The backup history disappearing turns "stale" into "never ran": a
    # different failure, so the chain has to record it.
    AgentCommand.objects.all().delete()
    _report, error = _run()
    assert error

    assert _events(deployment, "cluster.etcd_backup.problem").count() == 2


@pytest.mark.django_db
def test_repeat_after_hours_zero_audits_every_run():
    _, _, _, deployment, _ = _workspace()

    for _ in range(2):
        _report, error = _run(**{"repeat_after_hours": 0})
        assert error

    assert _events(deployment, "cluster.etcd_backup.problem").count() == 2


@pytest.mark.django_db
def test_a_cluster_that_recovers_gets_one_event_that_says_so():
    tenant, project, target, deployment, user = _workspace()
    _report, error = _run()
    assert error

    _backup(tenant, project, target, deployment, user, hours_ago=1)
    report, error = _run()

    assert error == ""
    assert report["problems"] == []
    recovered = _events(deployment, "cluster.etcd_backup.recovered").get()
    assert recovered.metadata["previous_code"] == "ETCD_BACKUP_NOT_RUN_YET"
    # A second clean probe does not celebrate the same recovery twice.
    _run()
    assert _events(deployment, "cluster.etcd_backup.recovered").count() == 1


@pytest.mark.django_db
def test_old_backups_with_a_silent_scheduler_name_the_scheduler():
    tenant, project, target, deployment, user = _workspace()
    _backup(tenant, project, target, deployment, user, hours_ago=100)
    _scheduler_events(tenant).delete()
    _beat(tenant, hours_ago=5)

    report, error = _run()

    assert error
    assert report["clusters"][0]["code"] == "ETCD_BACKUP_SCHEDULER_SILENT"
    assert report["clusters"][0]["scheduler_age_hours"] >= 5


@pytest.mark.django_db
def test_old_backups_with_a_live_scheduler_stay_stale():
    tenant, project, target, deployment, user = _workspace()
    _backup(tenant, project, target, deployment, user, hours_ago=100)

    report, error = _run()

    assert error
    assert report["clusters"][0]["code"] == "ETCD_BACKUP_STALE"


@pytest.mark.django_db
def test_fresh_backups_are_quiet_even_when_the_scheduler_has_never_reported():
    tenant, project, target, deployment, user = _workspace()
    _scheduler_events(tenant).delete()
    _backup(tenant, project, target, deployment, user, hours_ago=1)

    report, error = _run()

    assert error == ""
    assert report["problems"] == []
