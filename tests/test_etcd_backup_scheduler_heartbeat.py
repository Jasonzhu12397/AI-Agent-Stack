"""The scheduler heartbeat: one evaluation record per cluster, borrowed by none.

The heartbeat says "this cluster was evaluated in this hour bucket, and here is
what happened". It must not say "some command ran on this tenant", which let one
queued cluster lend its freshness to every other cluster in the tenant.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from core.etcd_backup_queue import hour_bucket
from core.etcd_backup_state import etcd_backup_state
from core.models import (
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

EVENT = "cluster.etcd_backup.scheduler.run"
RESOURCE_TYPE = "etcd_backup_scheduler"


def _workspace():
    user = get_user_model().objects.create_user(
        username="heartbeat-admin", password="x"
    )
    tenant = Tenant.objects.create(name="Heartbeat tenant", slug="heartbeat-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Heartbeat project", slug="heartbeat-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-heartbeat",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-heartbeat",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        credential_hash="t" * 64,
    )
    return user, tenant, project, eph, target


def _deployment(tenant, project, eph, target, user, *, name, **overrides):
    return ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=eph,
        target_node=target,
        requested_by=user,
        cluster_name=name,
        kubernetes_version="v1.37.2",
        idempotency_key="heartbeat-" + name,
        status=ClusterDeployment.Status.SUCCEEDED,
        spec=overrides.pop("spec", {}),
    )


def _run(**options):
    stdout = StringIO()
    call_command("queue_etcd_backups", stdout=stdout, **options)
    return json.loads(stdout.getvalue())


def _events(deployment):
    return AuditEvent.objects.filter(
        tenant=deployment.tenant,
        event_type=EVENT,
        resource_type=RESOURCE_TYPE,
        resource_id=str(deployment.id),
    )


@pytest.mark.django_db
def test_the_heartbeat_is_recorded_per_deployment_with_its_own_outcome():
    user, tenant, project, eph, target = _workspace()
    queued_deployment = _deployment(
        tenant, project, eph, target, user, name="lab"
    )
    skipped_deployment = _deployment(
        tenant,
        project,
        eph,
        target,
        user,
        name="web",
        spec={"kubernetes": {"etcd_backup": False}},
    )

    _run()

    queued_events = _events(queued_deployment)
    assert queued_events.count() == 1
    queued_event = queued_events.get()
    assert queued_event.metadata["bucket"] == hour_bucket()
    assert queued_event.metadata["result"] == "queued"
    assert queued_event.metadata["cluster_name"] == "lab"
    assert queued_event.metadata.get("command_id")

    skipped_events = _events(skipped_deployment)
    assert skipped_events.count() == 1
    skipped_event = skipped_events.get()
    assert skipped_event.metadata["bucket"] == hour_bucket()
    assert skipped_event.metadata["result"] == "skipped"
    assert skipped_event.metadata["reason"] == "disabled"

    # The record belongs to the cluster it evaluated, not to the tenant.
    assert AuditEvent.objects.filter(resource_id="").count() == 0


@pytest.mark.django_db
def test_a_cluster_does_not_borrow_another_cluster_heartbeat():
    user, tenant, project, eph, target = _workspace()
    first = _deployment(tenant, project, eph, target, user, name="lab")
    second = _deployment(tenant, project, eph, target, user, name="web")

    _run()
    # The second cluster's own evaluation record disappears; the first cluster
    # must not be the heartbeat the second one reports.
    _events(second).delete()

    second_state = etcd_backup_state(second)
    assert second_state["scheduler_last_run"] is None
    assert second_state["scheduler_age_hours"] is None

    first_state = etcd_backup_state(first)
    assert first_state["scheduler_last_run"] is not None


@pytest.mark.django_db
def test_repeating_in_the_same_bucket_writes_no_extra_heartbeat():
    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, name="lab")

    _run()
    _run()

    assert _events(deployment).count() == 1


@pytest.mark.django_db
def test_a_dry_run_writes_no_heartbeat():
    user, tenant, project, eph, target = _workspace()
    _deployment(tenant, project, eph, target, user, name="lab")

    _run(dry_run=True)

    assert AuditEvent.objects.filter(event_type=EVENT).count() == 0


@pytest.mark.django_db
def test_a_legacy_tenant_level_heartbeat_does_not_count_as_a_beat():
    from core.audit import record_audit_event

    user, tenant, project, eph, target = _workspace()
    deployment = _deployment(tenant, project, eph, target, user, name="lab")

    # The old schema stored one tenant-level marker with an empty resource id.
    # Such a row must not raise, and must not be read as this cluster's own beat.
    record_audit_event(
        None,
        tenant,
        EVENT,
        RESOURCE_TYPE,
        "",
        outcome=AuditEvent.Outcome.SUCCESS,
        metadata={"bucket": hour_bucket()},
    )

    state = etcd_backup_state(deployment)
    assert state["scheduler_last_run"] is None
    assert state["scheduler_age_hours"] is None


@pytest.mark.django_db
def test_a_policy_skipped_cluster_is_not_reported_as_scheduler_silent():
    user, tenant, project, eph, target = _workspace()
    # No Target Agent: the scheduler evaluates the cluster and skips it.
    deployment = _deployment(
        tenant, project, eph, None, user, name="lab"
    )

    report = _run()
    assert report["skipped"][0]["deployment_id"] == str(deployment.id)

    stdout = StringIO()
    try:
        call_command("check_etcd_backups", stdout=stdout, no_alert=True)
    except CommandError:
        # The agentless cluster is still a problem; the probe exits non-zero,
        # but the report it wrote is what the assertion reads.
        pass
    watchdog = json.loads(stdout.getvalue())

    entry = next(
        item for item in watchdog["clusters"] if item["deployment_id"] == str(deployment.id)
    )
    assert entry["code"] != "ETCD_BACKUP_SCHEDULER_SILENT"
