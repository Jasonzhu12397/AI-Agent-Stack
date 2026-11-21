"""Rolling a control plane back by restoring the snapshot the upgrade took.

The upgrade path always writes an etcd snapshot before it touches anything, so a
failed upgrade can be undone by putting that data back. This contract only
describes the request; it refuses anything the executor cannot do safely rather
than letting a half-specified rollback reach the cluster.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

ACTION = "cluster.kubernetes.version.rollback"
#: ``simulation`` returns the exact commands; ``execute`` runs them. Both need an
#: approval, and ``execute`` additionally needs the cluster name echoed back.
EXECUTION_MODES: tuple[str, ...] = ("simulation", "execute")
PAYLOAD_KEYS = frozenset(
    {"cluster_name", "snapshot_path", "execution_mode", "confirm_cluster_name"}
)

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
#: Absolute path inside the snapshot directory, no traversal, ``.db`` only.
_SNAPSHOT_PATH = re.compile(r"^/[A-Za-z0-9._/-]+\.db$")


class KubernetesRollbackError(ValueError):
    """A rollback request that will not be executed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def validate_request(
    payload: Any, *, cluster_name: str | None = None
) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise KubernetesRollbackError(
            "ROLLBACK_PAYLOAD_INVALID", "payload must be a JSON object"
        )
    unknown = sorted(set(payload) - PAYLOAD_KEYS)
    if unknown:
        raise KubernetesRollbackError(
            "ROLLBACK_PAYLOAD_INVALID", f"unknown payload keys: {', '.join(unknown)}"
        )
    cluster = str(payload.get("cluster_name") or "").strip()
    if not cluster or not _NAME.match(cluster) or len(cluster) > 63:
        raise KubernetesRollbackError(
            "ROLLBACK_CLUSTER_NAME_INVALID", "cluster_name must be a DNS label"
        )
    expected = (cluster_name or os.environ.get("TARGET_CLUSTER_NAME") or "").strip()
    if expected and expected != cluster:
        raise KubernetesRollbackError(
            "ROLLBACK_CLUSTER_NAME_MISMATCH",
            f"this agent hosts {expected}, not {cluster}",
        )
    snapshot = str(payload.get("snapshot_path") or "").strip()
    if not snapshot or not _SNAPSHOT_PATH.match(snapshot) or ".." in snapshot:
        raise KubernetesRollbackError(
            "ROLLBACK_SNAPSHOT_PATH_INVALID",
            "snapshot_path must be an absolute .db path without traversal",
        )
    mode = str(payload.get("execution_mode") or "").strip()
    if mode not in EXECUTION_MODES:
        raise KubernetesRollbackError(
            "ROLLBACK_EXECUTION_MODE_INVALID", "execution_mode must be simulation or execute"
        )
    confirm = str(payload.get("confirm_cluster_name") or "").strip()
    if mode == "execute" and confirm != cluster:
        raise KubernetesRollbackError(
            "ROLLBACK_CONFIRMATION_MISMATCH",
            "execute requires confirm_cluster_name to equal cluster_name",
        )
    return {
        "cluster_name": cluster,
        "snapshot_path": snapshot,
        "execution_mode": mode,
        "confirm_cluster_name": confirm,
    }


DEFAULT_ETCD_DATA_DIR = "/var/lib/etcd"


def planned_steps(
    request: dict[str, str], *, restore_dir: str = ""
) -> list[dict[str, Any]]:
    """Every step with the commands it would run, derived from one request."""

    restore = restore_dir or f"{DEFAULT_ETCD_DATA_DIR}/rollback-<utc>"
    member_backup = f"{DEFAULT_ETCD_DATA_DIR}/member.pre-rollback-<utc>"
    etcd_pod = "<etcd-pod>"
    return [
        {
            "sequence": 1,
            "name": "preflight",
            "status": "not_started",
            "commands": [
                "kubectl get nodes -o json",
                f"kubectl -n kube-system exec {etcd_pod} -- etcdctl snapshot status "
                f"{request['snapshot_path']} --write-out=json",
            ],
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 2,
            "name": "etcd_restore",
            "status": "not_started",
            "commands": [
                f"kubectl -n kube-system exec {etcd_pod} -- etcdctl snapshot restore "
                f"{request['snapshot_path']} --data-dir={restore}",
                f"sudo test -d {restore}/member",
            ],
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 3,
            "name": "data_swap",
            "status": "not_started",
            "commands": [
                "sudo systemctl stop kubelet",
                f"sudo mv {DEFAULT_ETCD_DATA_DIR}/member {member_backup}",
                f"sudo mv {restore}/member {DEFAULT_ETCD_DATA_DIR}/member",
            ],
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 4,
            "name": "kubelet_restart",
            "status": "not_started",
            "commands": ["sudo systemctl start kubelet", "kubectl get nodes -o json"],
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 5,
            "name": "postflight",
            "status": "not_started",
            "commands": [
                "kubectl get nodes -o json",
                f"kubectl -n kube-system exec {etcd_pod} -- etcdctl "
                "--endpoints=https://127.0.0.1:2379 endpoint health --cluster",
            ],
            "exit_code": None,
            "detail": "",
        },
    ]


def simulation_plan(request: dict[str, str]) -> dict[str, Any]:
    """The same steps the executor would run, without touching anything."""

    return {
        "cluster_name": request["cluster_name"],
        "snapshot_path": request["snapshot_path"],
        "execution_mode": "simulation",
        "approval_required": True,
        "rollback_simulated": True,
        "rolled_back": False,
        "requires_manual_execution": True,
        "performed_steps": [],
        "steps": planned_steps(request),
        "preflight": {"checks": [], "blocked": None},
        "failure_code": None,
    }
