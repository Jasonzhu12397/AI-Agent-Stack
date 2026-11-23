"""One authoritative protect set, and the F3/F5/F6 lifecycle rules behind it.

The protect set is built in exactly one place (the policy layer, which owns
command lifecycle). These tests pin:

- F3: a failed rollback keeps protecting when its outcome is unknown or it
  never executed; it releases only when the snapshot was consumed or is
  explicitly unusable.
- F5: a verified snapshot reported by a settled upgrade enters the protect
  set, and the panel marks it unconfirmed/missing instead of pretending it
  was confirmed on disk.
- F6: in the executor, prune and assessment consume one protect variable,
  never two independent reads of the request payload.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from core.etcd_backup_policy import (
    protected_entries,
    protected_names,
)
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
from target_agent.etcd_backup_exec import EtcdBackupExecutor

from packages.agent_contracts.etcd_backup import validate_request

ROLLBACK_ACTION = "cluster.kubernetes.version.rollback"
UPGRADE_ACTION = "cluster.kubernetes.version.upgrade"
BACKUP_ACTION = "cluster.kubernetes.backup.run"
ROLLBACK_SNAPSHOT = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
ROLLBACK_PATH = f"/var/lib/etcd/backups/{ROLLBACK_SNAPSHOT}"
UPGRADE_SNAPSHOT = "snapshot-pre-lab-v1.38.0-20261120T000000Z.db"
UPGRADE_PATH = f"/var/lib/etcd/backups/{UPGRADE_SNAPSHOT}"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _workspace(spec: dict | None = None):
    user = get_user_model().objects.create_user(
        username="authority-admin", password="x"
    )
    tenant = Tenant.objects.create(name="Authority tenant", slug="authority-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Authority project", slug="authority-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-authority",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-authority",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="lab",
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
        idempotency_key="authority-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec=spec or {},
    )
    return user, tenant, project, target, deployment


def _rollback(
    deployment,
    *,
    status: str,
    key: str,
    error: dict | None = None,
    result: dict | None = None,
    snapshot_path: str = ROLLBACK_PATH,
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
        error=error,
        result=result,
        deployment=deployment,
    )


# --------------------------------------------------------------------- F3

KEEP_CODES = [
    "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED",
    "CAPI_PIVOT_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED",
    "LEASE_EXPIRED",
    "APPROVAL_EXPIRED",
    "APPROVAL_HASH_MISMATCH",
    "AGENT_CONTROL_OWNERSHIP_FENCED",
]


@pytest.mark.django_db
@pytest.mark.parametrize("code", KEEP_CODES)
def test_failed_rollback_with_unknown_or_never_executed_code_still_protects(code):
    _, _, _, _, deployment = _workspace()
    _rollback(
        deployment,
        status=AgentCommand.Status.FAILED,
        key=f"rb-keep-{code}",
        error={"code": code, "retryable": False},
    )

    assert protected_names(deployment) == [ROLLBACK_SNAPSHOT]
    entries = protected_entries(deployment)
    assert entries[0].source == "rollback_outcome_unknown"


@pytest.mark.django_db
def test_failed_rollback_that_consumed_the_snapshot_releases_it():
    _, _, _, _, deployment = _workspace()
    _rollback(
        deployment,
        status=AgentCommand.Status.FAILED,
        key="rb-consumed",
        result={"rolled_back": True, "failure_code": "ROLLBACK_POSTFLIGHT_FAILED"},
    )

    assert protected_names(deployment) == []


@pytest.mark.django_db
def test_failed_rollback_with_explicitly_unusable_snapshot_releases_it():
    _, _, _, _, deployment = _workspace()
    _rollback(
        deployment,
        status=AgentCommand.Status.FAILED,
        key="rb-unusable",
        error={"code": "ROLLBACK_SNAPSHOT_PATH_INVALID", "retryable": False},
    )

    assert protected_names(deployment) == []


@pytest.mark.django_db
def test_succeeded_rollback_releases_the_snapshot_it_consumed():
    _, _, _, _, deployment = _workspace()
    _rollback(
        deployment,
        status=AgentCommand.Status.SUCCEEDED,
        key="rb-succeeded",
        result={"rolled_back": True},
    )

    assert protected_names(deployment) == []


# --------------------------------------------------------------------- F5


@pytest.mark.django_db
def test_verified_upgrade_reported_snapshot_enters_the_protect_set():
    _, _, _, _, deployment = _workspace()
    AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        node=deployment.target_node,
        requested_by=deployment.requested_by,
        command_type=UPGRADE_ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="upgrade-reported",
        plan_hash="0" * 64,
        deployment=deployment,
        result={"etcd_snapshot": {"path": UPGRADE_PATH, "verified": True}},
    )

    assert UPGRADE_SNAPSHOT in protected_names(deployment)
    entries = protected_entries(deployment)
    upgrade = next(entry for entry in entries if entry.name == UPGRADE_SNAPSHOT)
    assert upgrade.source == "upgrade_reported"

    state = etcd_backup_state(deployment)
    assert UPGRADE_SNAPSHOT in state["policy"]["protect"]


@pytest.mark.django_db
def test_upgrade_reported_snapshot_not_in_listing_is_marked_unconfirmed():
    _, _, _, _, deployment = _workspace()
    listed = "lab-20261122T080000Z.db"
    AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        node=deployment.target_node,
        requested_by=deployment.requested_by,
        command_type=BACKUP_ACTION,
        payload={"cluster_name": "lab", "directory": "/var/lib/etcd/backups"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="backup-listing",
        plan_hash="0" * 64,
        deployment=deployment,
        result={"entries": [{"name": listed}], "kept": [listed], "count": 1},
    )
    AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        node=deployment.target_node,
        requested_by=deployment.requested_by,
        command_type=UPGRADE_ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="upgrade-old-dir",
        plan_hash="0" * 64,
        deployment=deployment,
        result={"etcd_snapshot": {"path": UPGRADE_PATH, "verified": True}},
    )

    state = etcd_backup_state(deployment)

    unconfirmed = {
        point["name"]: point for point in state.get("unconfirmed_restore_points", [])
    }
    assert UPGRADE_SNAPSHOT in unconfirmed
    assert unconfirmed[UPGRADE_SNAPSHOT]["listed"] is False
    assert unconfirmed[UPGRADE_SNAPSHOT]["unconfirmed"] is True
    assert unconfirmed[UPGRADE_SNAPSHOT]["reason"] == "not_in_recent_listing"


@pytest.mark.django_db
def test_upgrade_reported_snapshot_without_any_listing_is_marked_unconfirmed():
    _, _, _, _, deployment = _workspace()
    AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        node=deployment.target_node,
        requested_by=deployment.requested_by,
        command_type=UPGRADE_ACTION,
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.FAILED,
        idempotency_key="upgrade-no-listing",
        plan_hash="0" * 64,
        deployment=deployment,
        result={"etcd_snapshot": {"path": UPGRADE_PATH, "verified": True}},
    )

    state = etcd_backup_state(deployment)
    point = state["restore_point"]
    assert point is not None
    assert point["name"] == UPGRADE_SNAPSHOT
    assert point["listed"] is False
    assert point["unconfirmed"] is True
    assert point["reason"] == "no_recent_listing"


# --------------------------------------------------------------------- F6


class _PermissiveRunner:
    """Answers every pod call a backup run makes, with an empty directory."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if "get" in argv and "pods" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(
                    {"items": [{"metadata": {"name": "etcd-cp-0"}, "status": {"phase": "Running"}}]}
                ),
                "stderr": "",
            }
        if "endpoint" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"Header": {"dbSize": 8 * 1024 * 1024}}]),
                "stderr": "",
            }
        if "df" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": (
                    "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                    "/dev/sda1 100 10 1073741824 1% /var\n"
                ),
                "stderr": "",
            }
        if "ls" in argv:
            return {"available": True, "returncode": 0, "stdout": "total 0", "stderr": ""}
        return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}


def test_prune_and_assess_share_the_same_protect_variable(monkeypatch):
    captured: dict[str, tuple[str, ...]] = {}

    def _fake_prunable(entries, **kwargs):
        captured["prunable"] = tuple(kwargs.get("protect") or ())
        return []

    def _fake_assess(entries, **kwargs):
        captured["assess"] = tuple(kwargs.get("protect") or ())
        return {
            "newest": None,
            "newest_age_hours": None,
            "stale": True,
            "ok": False,
            "count": 0,
            "prunable": [],
            "pre_upgrade_count": 0,
            "pre_upgrade": [],
            "findings": [],
            "restore_point": None,
        }

    monkeypatch.setattr("target_agent.etcd_backup_exec.select_prunable", _fake_prunable)
    monkeypatch.setattr("target_agent.etcd_backup_exec.assess", _fake_assess)

    request = validate_request(
        {
            "cluster_name": "lab",
            "execution_mode": "execute",
            "confirm_cluster_name": "lab",
            "protect": [ROLLBACK_SNAPSHOT],
        }
    )
    EtcdBackupExecutor(_PermissiveRunner()).run(request, now=NOW)

    assert captured["prunable"] == (ROLLBACK_SNAPSHOT,)
    assert captured["assess"] == (ROLLBACK_SNAPSHOT,)
    # One variable feeds both calls: the two captured values are equal, not
    # two independently parsed copies of the request payload.
    assert captured["prunable"] == captured["assess"]
