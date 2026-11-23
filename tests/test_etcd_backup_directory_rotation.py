"""Backup directory rotation: snapshots in a retired directory stay in scope.

When the policy directory moves from A to B, snapshots left in A must neither
leak on the disk nor disappear from the panel. Retention and the state view
derive the candidate location set from the records this cluster already has,
instead of trusting only the current spec directory.
"""

from __future__ import annotations

import datetime
import json

import pytest
from core.etcd_backup_state import etcd_backup_state
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

from apps.target_agent.etcd_backup_exec import EtcdBackupExecutor
from packages.agent_contracts.etcd_backup import validate_request

NOW = datetime.datetime(2026, 11, 22, 3, 0, tzinfo=datetime.UTC)

DIR_A = "/var/lib/etcd/backups-a"
DIR_B = "/var/lib/etcd/backups-b"


def _name(hours_ago: int, cluster: str = "lab") -> str:
    stamp = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")
    return f"{cluster}-{stamp}.db"


class MapRunner:
    """Answers pod calls, reading the file listing per requested directory."""

    def __init__(self, files_by_dir: dict[str, list[str]]) -> None:
        self.files_by_dir = files_by_dir
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
        if "find" in argv:
            return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}
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
                    "/dev/sda1 10 10 4294967296 1% /var\n"
                ),
                "stderr": "",
            }
        if "ls" in argv:
            directory = argv[-1]
            lines = ["total 0"]
            for index, name in enumerate(self.files_by_dir.get(directory, [])):
                lines.append(
                    f"-rw-r--r--    1 root     root       {1000 + index} Jan  1 00:00 {name}"
                )
            return {"available": True, "returncode": 0, "stdout": "\n".join(lines), "stderr": ""}
        return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}


def _execute_request(**overrides):
    payload = {
        "cluster_name": "lab",
        "execution_mode": "execute",
        "confirm_cluster_name": "lab",
    }
    payload.update(overrides)
    return validate_request(payload)


def _workspace():
    user = get_user_model().objects.create_user(
        username="rotation-admin", password="x"
    )
    tenant = Tenant.objects.create(name="Rotation tenant", slug="rotation-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Rotation project", slug="rotation-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    eph = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-rotation",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="e" * 64,
    )
    target = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-rotation",
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
        idempotency_key="rotation-key",
        status=ClusterDeployment.Status.SUCCEEDED,
        spec={"kubernetes": {"etcd_backup_directory": DIR_B}},
    )
    return user, tenant, project, target, deployment


@pytest.mark.django_db
def test_old_directory_snapshots_stay_in_retention_decision_after_rotation():
    old = _name(6)
    recent_a = _name(4)
    recent_b = _name(5)
    runner = MapRunner(
        {
            DIR_A: [old, recent_a],
            DIR_B: [recent_b],
        }
    )
    request = _execute_request(keep_max_age_hours=5, directories=[DIR_A, DIR_B])

    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    # The over-age snapshot in the retired directory enters the retention
    # decision and is pruned from its own directory; the two newest survive.
    assert result["pruned"] == [old]
    pruned_entry = next(entry for entry in result["pruned_entries"] if entry["name"] == old)
    assert pruned_entry["directory"] == DIR_A
    rm_calls = [call for call in runner.calls if "rm" in call]
    assert any(
        DIR_A in argument and old in argument
        for call in rm_calls
        for argument in call
    )
    kept_names = [entry["name"] for entry in result["kept"]]
    assert recent_a in kept_names
    assert recent_b in kept_names
    # The active directory's files are not touched by the retired-directory pass.
    assert not any(
        recent_b in argument for call in rm_calls for argument in call
    )


@pytest.mark.django_db
def test_state_lists_both_directories_after_rotation():
    user, tenant, project, target, deployment = _workspace()
    # A previous cycle queued against the retired directory A.
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.backup.run",
        payload={"cluster_name": "lab", "directory": DIR_A},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="rotation-history",
        plan_hash="0" * 64,
        deployment=deployment,
    )
    AuditEvent.objects.create(
        tenant=tenant,
        event_type="cluster.etcd_backup.policy.updated",
        resource_type="cluster_deployment",
        resource_id=str(deployment.id),
        metadata={"policy": {"etcd_backup_directory": DIR_A}},
        event_hash="a" * 64,
    )

    state = etcd_backup_state(deployment)

    assert DIR_A in state["directories"]
    assert DIR_B in state["directories"]


@pytest.mark.django_db
def test_reported_snapshot_in_old_directory_is_flagged_unconfirmed():
    user, tenant, project, target, deployment = _workspace()
    snapshot = "snapshot-pre-lab-v1.37.2-20261101T000000Z.db"
    AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target,
        requested_by=user,
        command_type="cluster.kubernetes.version.upgrade",
        payload={"cluster_name": "lab"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="rotation-upgrade",
        plan_hash="0" * 64,
        deployment=deployment,
        result={"etcd_snapshot": {"path": f"{DIR_A}/{snapshot}", "verified": True}},
    )

    state = etcd_backup_state(deployment)

    point = state["restore_point"]
    assert point["name"] == snapshot
    assert point["listed"] is False
    assert point["unconfirmed"] is True
