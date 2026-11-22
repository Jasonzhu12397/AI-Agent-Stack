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


def snapshot_pod_path_problem(path: str) -> str:
    """Why the etcd pod cannot read a snapshot stored at ``path``, or "".

    The etcd static pod mounts the host data directory and nothing else. A
    snapshot anywhere else exists on the host and is invisible inside the pod,
    so the restore that reads it there fails after the cluster has already been
    touched. The upgrade and the rollback both ask this one question.
    """

    mount = DEFAULT_ETCD_DATA_DIR.rstrip("/")
    if path.startswith(mount + "/"):
        return ""
    return (
        f"{path} is outside {mount}, the only host path the etcd pod mounts; "
        f"move the snapshot under {mount}/backups"
    )


def planned_steps(
    request: dict[str, str],
    *,
    restore_dir: str = "",
    members: tuple[dict[str, str], ...] = (),
) -> list[dict[str, Any]]:
    """Every step with the commands it would run, derived from one request.

    ``members`` describes a stacked etcd with more than one control plane: each
    entry carries the member name, the node it runs on, the etcd pod on that
    node and its peer URL. With one member (or none) the plan is the local
    single-node sequence; with several, every mutation is listed per member so
    the plan says exactly which node is touched.
    """

    restore = restore_dir or f"{DEFAULT_ETCD_DATA_DIR}/rollback-<utc>"
    member_backup = f"{DEFAULT_ETCD_DATA_DIR}/member.pre-rollback-<utc>"
    etcd_pod = "<etcd-pod>"
    if len(members) > 1:
        return _multi_member_steps(request, restore, member_backup, members)
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


def _initial_cluster(members: tuple[dict[str, str], ...]) -> str:
    return ",".join(f"{item['name']}={item['peer_url']}" for item in members)


def _member_step_preflight(members: tuple[dict[str, str], ...], snapshot: str) -> list[str]:
    commands = [
        "kubectl get nodes -o json",
        "kubectl -n kube-system get pods -l component=etcd -o json",
    ]
    for item in members:
        commands.append(
            f"kubectl -n kube-system exec {item['pod']} -- etcdctl snapshot status "
            f"{snapshot} --write-out=json"
        )
    return commands


def _multi_member_steps(
    request: dict[str, str],
    restore: str,
    member_backup: str,
    members: tuple[dict[str, str], ...],
) -> list[dict[str, Any]]:
    """One plan for a stacked etcd: every member is restored from the same file.

    Restoring a single member of a stacked etcd leaves the cluster with data
    that disagrees with its peers, so the snapshot is put back on all of them,
    their data directories are swapped while every kubelet is stopped, and they
    are started one at a time so the quorum comes up in a known order.
    """

    cluster = _initial_cluster(members)
    restore_commands: list[str] = []
    swap_commands: list[str] = []
    for item in members:
        restore_commands.append(
            f"kubectl -n kube-system exec {item['pod']} -- etcdctl snapshot restore "
            f"{request['snapshot_path']} --data-dir={restore} --name={item['name']} "
            f"--initial-cluster={cluster} "
            f"--initial-advertise-peer-urls={item['peer_url']}"
        )
        restore_commands.append(f"sudo test -d {restore}/member")
        swap_commands.append(f"sudo mv {DEFAULT_ETCD_DATA_DIR}/member {member_backup}")
        swap_commands.append(f"sudo mv {restore}/member {DEFAULT_ETCD_DATA_DIR}/member")
    stop_all = ["sudo systemctl stop kubelet"]
    start_commands = ["sudo systemctl start kubelet", "kubectl get nodes -o json"]
    return [
        {
            "sequence": 1,
            "name": "preflight",
            "status": "not_started",
            "commands": _member_step_preflight(members, request["snapshot_path"]),
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 2,
            "name": "etcd_restore",
            "status": "not_started",
            "commands": restore_commands,
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 3,
            "name": "data_swap",
            "status": "not_started",
            "commands": [*stop_all, *swap_commands],
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 4,
            "name": "kubelet_restart",
            "status": "not_started",
            "commands": start_commands,
            "exit_code": None,
            "detail": "",
        },
        {
            "sequence": 5,
            "name": "postflight",
            "status": "not_started",
            "commands": [
                "kubectl get nodes -o json",
                "kubectl -n kube-system exec <etcd-pod> -- etcdctl "
                "--endpoints=https://127.0.0.1:2379 endpoint health --cluster",
            ],
            "exit_code": None,
            "detail": "",
        },
    ]


def simulation_plan(
    request: dict[str, str],
    *,
    members: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
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
        "steps": planned_steps(request, members=members),
        "preflight": {"checks": [], "blocked": None},
        "failure_code": None,
    }
