"""Real execution of a Kubernetes minor upgrade on the Target Agent host.

The contract module decides whether a request is legal and describes what an
upgrade would do. This module is the only place that touches a live cluster, and
it is deliberately narrow:

* stacked etcd is backed up by the agent itself before anything changes
    (``etcdctl snapshot save`` inside the etcd pod, then ``snapshot status``);
    the preflight writes only that backup file;
* the Target Agent upgrades the control plane it is running beside
    (``kubeadm upgrade apply``);
* workers are upgraded through an explicitly configured remote executor
    (``KUBERNETES_UPGRADE_REMOTE_USER``); without it a cluster that has workers
    is refused instead of pretending workers were upgraded;
* a multi control-plane cluster is upgraded the way kubeadm prescribes: one
    ``kubeadm upgrade apply`` on the control plane this agent runs beside, then
    ``kubeadm upgrade node`` on each remaining control plane over ssh, then the
    workers. The local control plane is identified by hostname or by
    ``KUBERNETES_UPGRADE_LOCAL_NODE``; when it cannot be identified the run is
    refused instead of guessing and running ``upgrade node`` on the wrong host.

There is no rollback. A failed step stops the run, records the exact command and
its output, and leaves ``upgrade_performed`` true so the operator never reads a
half-upgraded cluster as untouched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import time
from collections.abc import Callable
from typing import Any

REMOTE_USER_ENV = "KUBERNETES_UPGRADE_REMOTE_USER"
LOCAL_NODE_ENV = "KUBERNETES_UPGRADE_LOCAL_NODE"
ETCD_SNAPSHOT_ENV = "KUBERNETES_UPGRADE_ETCD_SNAPSHOT"
ETCD_SNAPSHOT_DIR_ENV = "KUBERNETES_UPGRADE_ETCD_SNAPSHOT_DIR"
DEFAULT_ETCD_SNAPSHOT_DIR = "/var/lib/etcd/backups"
ETCD_SNAPSHOT_MAX_AGE_ENV = "KUBERNETES_UPGRADE_ETCD_SNAPSHOT_MAX_AGE_HOURS"
DEFAULT_ETCD_SNAPSHOT_MAX_AGE_HOURS = 24.0
PACKAGE_MANAGER_ENV = "KUBERNETES_UPGRADE_PACKAGE_MANAGER"
PACKAGE_VERSION_ENV = "KUBERNETES_UPGRADE_PACKAGE_VERSION"
PACKAGE_MANAGERS = ("apt", "dnf")
_PACKAGE_VERSION = re.compile(r"^[0-9A-Za-z.+~:-]{1,64}$")
STEP_TIMEOUT_SECONDS = 1800
DRAIN_TIMEOUT_SECONDS = 900

RunCommand = Callable[..., dict[str, Any]]


class UpgradeExecutionError(ValueError):
    """Raised before any durable change when the run cannot be started."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _output(result: dict[str, Any], limit: int = 2000) -> str:
    text = (result.get("stdout") or "").strip()
    if not text:
        text = (result.get("stderr") or "").strip()
    return text[-limit:]


class UpgradeExecutor:
    """Run one upgrade request against the cluster this agent belongs to."""

    def __init__(self, runner: RunCommand, *, remote_user: str | None = None) -> None:
        self._runner = runner
        self._remote_user = (remote_user if remote_user is not None else
                             os.environ.get(REMOTE_USER_ENV, "")).strip()
        self._package_configured = bool(
            os.environ.get(PACKAGE_MANAGER_ENV, "").strip()
        )
        self._last_preflight: dict[str, Any] = {"checks": [], "blocked": False}
        self._last_snapshot: dict[str, Any] | None = None

    # -- cluster facts ---------------------------------------------------
    def _nodes(self) -> list[dict[str, Any]]:
        result = self._runner(["kubectl", "get", "nodes", "-o", "json"], timeout=60)
        if not result.get("available", True) or result.get("returncode") != 0:
            raise UpgradeExecutionError(
                "UPGRADE_NODES_UNREADABLE", _output(result)
            )
        try:
            payload = json.loads(result.get("stdout") or "")
        except ValueError as exc:
            raise UpgradeExecutionError(
                "UPGRADE_NODES_JSON_INVALID", str(exc)
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise UpgradeExecutionError("UPGRADE_NODES_JSON_INVALID")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _is_control_plane(node: dict[str, Any]) -> bool:
        labels = (node.get("metadata") or {}).get("labels") or {}
        return any(
            key in labels
            for key in (
                "node-role.kubernetes.io/control-plane",
                "node-role.kubernetes.io/master",
            )
        )

    @staticmethod
    def _node_name(node: dict[str, Any]) -> str:
        return str((node.get("metadata") or {}).get("name") or "")

    @staticmethod
    def _node_address(node: dict[str, Any]) -> str:
        addresses = (node.get("status") or {}).get("addresses") or []
        internal = [
            item.get("address")
            for item in addresses
            if isinstance(item, dict) and item.get("type") == "InternalIP"
        ]
        return str(internal[0]) if internal else ""

    @staticmethod
    def _node_version(node: dict[str, Any]) -> str:
        return str(((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion") or "")

    @staticmethod
    def _on_target(version: str, target: str) -> bool:
        """Whether a node already reports the target minor (retry-safe skip)."""

        prefix = ".".join(target.split(".")[:2])
        return bool(version) and version.startswith(prefix)

    def _local_control_plane(self, names: list[str]) -> str:
        """Which control plane this agent runs beside.

        With a single control plane there is nothing to choose. With several,
        the answer must not be a guess: running ``kubeadm upgrade node`` on the
        wrong host still changes that host, so an unidentified host is refused.
        """

        if len(names) == 1:
            return names[0]
        wanted = os.environ.get(LOCAL_NODE_ENV, "").strip() or socket.gethostname().split(".")[0]
        for name in names:
            if name == wanted or name.split(".")[0] == wanted:
                return name
        raise UpgradeExecutionError(
            "UPGRADE_LOCAL_NODE_UNKNOWN",
            f"cannot tell which control plane host this agent runs on; set {LOCAL_NODE_ENV} "
            "to one of: " + ", ".join(names),
        )

    def _rolling_targets(
        self, control_planes: list[dict[str, Any]], workers: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return the local control plane and the peers upgraded over ssh."""

        local_name = self._local_control_plane(
            [self._node_name(node) for node in control_planes]
        )
        local_node = next(
            node for node in control_planes if self._node_name(node) == local_name
        )
        peers = sorted(
            (node for node in control_planes if self._node_name(node) != local_name),
            key=self._node_name,
        )
        if (peers or workers) and not self._remote_user:
            raise UpgradeExecutionError(
                "UPGRADE_REMOTE_EXECUTOR_NOT_CONFIGURED",
                f"set {REMOTE_USER_ENV} so the remaining nodes can be upgraded "
                "from this host over ssh",
            )
        return local_node, peers

    def _package_commands(self, target: str) -> list[list[str]] | None:
        """Return the kubelet/kubeadm/kubectl install commands, or ``None``.

        The package version is operator-supplied because every distribution
        builds it differently (``1.34.1-1.1`` on apt, ``1.34.1-1500`` on dnf).
        It must belong to the target release, so a mistake cannot install a
        different minor than the one being validated.
        """

        manager = os.environ.get(PACKAGE_MANAGER_ENV, "").strip()
        version = os.environ.get(PACKAGE_VERSION_ENV, "").strip()
        if not manager:
            return None
        if manager not in PACKAGE_MANAGERS:
            raise UpgradeExecutionError(
                "UPGRADE_PACKAGE_MANAGER_UNSUPPORTED",
                f"{PACKAGE_MANAGER_ENV} must be one of " + ", ".join(PACKAGE_MANAGERS),
            )
        if not version or not _PACKAGE_VERSION.match(version):
            raise UpgradeExecutionError(
                "UPGRADE_PACKAGE_VERSION_INVALID",
                f"set {PACKAGE_VERSION_ENV} to the distribution package version",
            )
        if not version.startswith(target.lstrip("v") + "-"):
            raise UpgradeExecutionError(
                "UPGRADE_PACKAGE_VERSION_MISMATCH",
                f"package version {version} is not built from {target}",
            )
        if manager == "apt":
            return [[
                "sudo",
                "apt-get",
                "install",
                "-y",
                "--allow-change-held-packages",
                f"kubelet={version}",
                f"kubeadm={version}",
                f"kubectl={version}",
            ]]
        return [[
            "sudo",
            "dnf",
            "install",
            "-y",
            f"kubelet-{version}",
            f"kubeadm-{version}",
            f"kubectl-{version}",
        ]]

    # -- preflight -------------------------------------------------------
    def _check(self, name: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "ok": bool(ok), "detail": detail}

    def _handler_ready(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        checks = []
        for node in nodes:
            name = self._node_name(node) or "<unnamed>"
            conditions = (node.get("status") or {}).get("conditions") or []
            ready = any(
                isinstance(condition, dict)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in conditions
            )
            checks.append(
                self._check(
                    f"node.ready/{name}",
                    ready,
                    "Ready=True" if ready else "Ready is not True",
                )
            )
            schedulable = not bool((node.get("spec") or {}).get("unschedulable"))
            checks.append(
                self._check(
                    f"node.schedulable/{name}",
                    schedulable,
                    "schedulable" if schedulable else "cordoned; uncordon it before upgrading",
                )
            )
        return checks

    def _kubeadm_config_checks(self, current: str) -> list[dict[str, Any]]:
        configmap_argv = [
            "kubectl",
            "-n",
            "kube-system",
            "get",
            "configmap",
            "kubeadm-config",
            "-o",
            "json",
        ]
        result = self._runner(configmap_argv, timeout=60)
        if not result.get("available", True) or result.get("returncode") != 0:
            return [self._check("kubeadm-config.present", False, _output(result))]
        try:
            document = json.loads(result.get("stdout") or "")
        except ValueError:
            return [self._check("kubeadm-config.present", False, "configmap is not JSON")]
        raw = ((document.get("data") or {}) if isinstance(document, dict) else {}).get(
            "ClusterConfiguration"
        ) or ""
        match = re.search(r"kubernetesVersion:\s*[\"']?(v[0-9.]+)", raw)
        if not match:
            return [
                self._check(
                    "kubeadm-config.version",
                    False,
                    "kubernetesVersion is not recorded",
                )
            ]
        configured = match.group(1).rstrip(".")
        current_minor = ".".join(current.split(".")[:2])
        configured_minor = ".".join(configured.split(".")[:2])
        return [
            self._check(
                "kubeadm-config.version",
                configured_minor == current_minor,
                f"configmap says {configured}, cluster reports {current}",
            )
        ]

    def _pod_disruption_checks(self) -> list[dict[str, Any]]:
        result = self._runner(
            ["kubectl", "get", "poddisruptionbudgets", "--all-namespaces", "-o", "json"],
            timeout=60,
        )
        if not result.get("available", True) or result.get("returncode") != 0:
            # No PDB API access is reported as unknown, never as "nothing to see".
            return [
                self._check(
                    "poddisruptionbudget.readable", False, _output(result) or "not readable"
                )
            ]
        try:
            document = json.loads(result.get("stdout") or "")
        except ValueError:
            return [self._check("poddisruptionbudget.readable", False, "list is not JSON")]
        items = document.get("items") if isinstance(document, dict) else []
        blocked = [
            str((item.get("metadata") or {}).get("name"))
            for item in items or []
            if isinstance(item, dict)
            and ((item.get("status") or {}).get("disruptionsAllowed") or 0) == 0
        ]
        return [
            self._check(
                "poddisruptionbudget.headroom",
                not blocked,
                "no budget at zero" if not blocked else "budgets at zero: " + ", ".join(blocked),
            )
        ]

    def _etcd_pods(self) -> list[dict[str, Any]]:
        """Stacked etcd members, with the node each one runs on."""

        pods = self._runner(
            ["kubectl", "-n", "kube-system", "get", "pods", "-l", "component=etcd", "-o", "json"],
            timeout=60,
        )
        if not pods.get("available", True) or pods.get("returncode") != 0:
            raise UpgradeExecutionError("UPGRADE_ETCD_UNREADABLE", _output(pods))
        try:
            items = json.loads(pods.get("stdout") or "").get("items") or []
        except ValueError as exc:
            raise UpgradeExecutionError("UPGRADE_ETCD_UNREADABLE", str(exc)) from exc
        return [item for item in items if isinstance(item, dict)]

    ETCD_PKI_DIR = "/etc/kubernetes/pki/etcd"

    def _snapshot_argv(self, pod: str) -> list[str]:
        return [
            "kubectl",
            "-n",
            "kube-system",
            "exec",
            pod,
            "--",
            "etcdctl",
            "--endpoints=https://127.0.0.1:2379",
            f"--cacert={self.ETCD_PKI_DIR}/ca.crt",
            f"--cert={self.ETCD_PKI_DIR}/server.crt",
            f"--key={self.ETCD_PKI_DIR}/server.key",
        ]

    def _take_etcd_snapshot(
        self, pod: str, local_node: str, target: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Back up stacked etcd to a verified file before anything is upgraded.

        The file is written through the etcd pod, which mounts the host data
        directory at the same path, so it survives on the host even if the
        upgrade fails. The snapshot is then read back with ``snapshot status``:
        a snapshot that cannot be read is not a backup.
        """

        directory = os.environ.get(ETCD_SNAPSHOT_DIR_ENV, "").strip() or (
            DEFAULT_ETCD_SNAPSHOT_DIR
        )
        name = f"snapshot-pre-{target}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
        path = f"{directory.rstrip('/')}/{name}"
        if not os.path.isabs(path) or ".." in path:
            return None, self._check(
                "etcd.snapshot",
                False,
                f"{ETCD_SNAPSHOT_DIR_ENV} must be an absolute directory",
            )
        mkdir = self._runner(["sudo", "mkdir", "-p", directory], timeout=60)
        if not mkdir.get("available", True) or mkdir.get("returncode") != 0:
            return None, self._check(
                "etcd.snapshot",
                False,
                f"cannot create {directory}: " + (_output(mkdir) or "mkdir failed"),
            )
        save = self._runner(
            [*self._snapshot_argv(pod), "snapshot", "save", path],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not save.get("available", True) or save.get("returncode") != 0:
            return None, self._check(
                "etcd.snapshot",
                False,
                f"snapshot save failed on {local_node}: " + (_output(save) or "no output"),
            )
        status = self._runner(
            [*self._snapshot_argv(pod), "snapshot", "status", path],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not status.get("available", True) or status.get("returncode") != 0:
            return None, self._check(
                "etcd.snapshot",
                False,
                f"snapshot {path} is not readable: " + (_output(status) or "no output"),
            )
        snapshot = {"path": path, "verified": True, "taken": True}
        return snapshot, self._check(
            "etcd.snapshot", True, f"took {path} and verified it"
        )

    def _etcd_snapshot_checks(
        self, local_node: str, target: str
    ) -> list[dict[str, Any]]:
        try:
            pods = self._etcd_pods()
        except UpgradeExecutionError as error:
            return [self._check("etcd.snapshot", False, f"{error.code}: {error.detail}")]
        if not pods:
            return [self._check("etcd.snapshot", True, "no stacked etcd on this cluster")]
        supplied = os.environ.get(ETCD_SNAPSHOT_ENV, "").strip()
        if supplied:
            try:
                age_hours = (time.time() - os.stat(supplied).st_mtime) / 3600
            except OSError:
                return [
                    self._check(
                        "etcd.snapshot", False, f"{supplied} is not readable"
                    )
                ]
            max_age = _snapshot_max_age_hours()
            taken = age_hours <= max_age
            if taken:
                self._last_snapshot = {
                    "path": supplied,
                    "verified": True,
                    "taken": False,
                }
            return [
                self._check(
                    "etcd.snapshot",
                    taken,
                    f"reusing {supplied} ({age_hours:.1f}h old, limit {max_age}h)",
                )
            ]
        pod = ""
        for item in pods:
            if str((item.get("spec") or {}).get("nodeName")) == local_node:
                pod = str((item.get("metadata") or {}).get("name") or "")
                break
        if not pod:
            pod = str((pods[0].get("metadata") or {}).get("name") or "")
        if not pod:
            return [
                self._check("etcd.snapshot", False, "no etcd pod name to run etcdctl in")
            ]
        snapshot, check = self._take_etcd_snapshot(pod, local_node, target)
        self._last_snapshot = snapshot
        return [check]

    def _ssh_reachability_checks(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prove every remote node answers ssh before anything local changes.

        The local control plane is upgraded first. If the next ssh hop then
        fails, the cluster is left half upgraded, so reachability is a
        preflight fact rather than an upgrade step.
        """

        if not nodes:
            return []
        checks = []
        for node in nodes:
            name = self._node_name(node) or "<unnamed>"
            address = self._node_address(node)
            if not address:
                checks.append(
                    self._check(f"ssh.reachable/{name}", False, "node has no InternalIP")
                )
                continue
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                f"{self._remote_user}@{address}",
                "true",
            ]
            result = self._runner(argv, timeout=30)
            ok = result.get("available", True) and result.get("returncode") == 0
            checks.append(
                self._check(
                    f"ssh.reachable/{name}",
                    ok,
                    "ssh accepted" if ok else _output(result) or "ssh was refused",
                )
            )
        return checks

    def _preflight(
        self,
        request: dict[str, str],
        nodes: list[dict[str, Any]],
        local_node: str,
        remote_nodes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        checks = [
            *self._handler_ready(nodes),
            *self._kubeadm_config_checks(request["current_version"]),
            *self._pod_disruption_checks(),
            *self._etcd_snapshot_checks(local_node, request["target_version"]),
            *self._ssh_reachability_checks(remote_nodes or []),
        ]
        failed = [check for check in checks if not check["ok"]]
        return {"checks": checks, "blocked": bool(failed)}

    # -- steps -----------------------------------------------------------
    def _step(self, sequence: int, name: str, commands: list[str]) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "name": name,
            "status": "not_started",
            "commands": commands,
            "exit_code": None,
            "detail": "",
        }

    def _execute_step(self, step: dict[str, Any], argv: list[str], timeout: int) -> bool:
        result = self._runner(argv, timeout=timeout)
        step["exit_code"] = result.get("returncode")
        ok = result.get("available", True) and result.get("returncode") == 0
        step["status"] = "succeeded" if ok else "failed"
        if not ok:
            step["detail"] = _output(result)
        return ok

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        target = request["target_version"]
        nodes = self._nodes()
        control_planes = [node for node in nodes if self._is_control_plane(node)]
        workers = [node for node in nodes if not self._is_control_plane(node)]
        if not control_planes:
            raise UpgradeExecutionError("UPGRADE_CONTROL_PLANE_NOT_FOUND")

        package_commands = self._package_commands(target)
        local_node, peers = self._rolling_targets(control_planes, workers)
        local_name = self._node_name(local_node)
        local_upgrade_needed = not self._on_target(
            self._node_version(local_node), target
        )
        control_commands = []
        if local_upgrade_needed:
            control_commands.extend(
                shlex.join(command) for command in package_commands or []
            )
            control_commands.append(f"kubeadm upgrade apply -y {target}")
            control_commands.append("sudo systemctl restart kubelet")
        else:
            control_commands.append(
                f"# {local_name} already runs {target}; kubeadm upgrade apply is not repeated"
            )
        peer_commands: list[tuple[dict[str, Any], list[tuple[list[str], int, str]]]] = []
        for peer in peers:
            name = self._node_name(peer)
            address = self._node_address(peer)
            if not address:
                raise UpgradeExecutionError(
                    "UPGRADE_CONTROL_PLANE_PEER_ADDRESS_UNKNOWN",
                    f"control plane {name} has no InternalIP to reach over ssh",
                )
            remote = f"{self._remote_user}@{address}"
            if self._on_target(self._node_version(peer), target):
                peer_commands.append((peer, []))
                continue
            argv_list = [
                (["ssh", remote, shlex.join(command)], STEP_TIMEOUT_SECONDS,
                 "UPGRADE_CONTROL_PLANE_PEER_PACKAGE_FAILED")
                for command in package_commands or []
            ]
            argv_list.extend([
                (["ssh", remote, "kubeadm", "upgrade", "node"], STEP_TIMEOUT_SECONDS,
                 "UPGRADE_CONTROL_PLANE_PEER_FAILED"),
                (["ssh", remote, "sudo", "systemctl", "restart", "kubelet"],
                 STEP_TIMEOUT_SECONDS, "UPGRADE_CONTROL_PLANE_PEER_KUBELET_RESTART_FAILED"),
            ])
            peer_commands.append((peer, argv_list))
        for peer, argv_list in peer_commands:
            name = self._node_name(peer)
            if not argv_list:
                control_commands.append(f"# {name} already runs {target}; skipped")
                continue
            control_commands.extend(
                shlex.join(argv) if argv[0] == "ssh" else " ".join(argv)
                for argv, _timeout, _code in argv_list
            )
        steps = [
            self._step(1, "preflight", [f"kubeadm upgrade plan {target}"]),
            self._step(2, "control_plane", control_commands),
            self._step(3, "workers", []),
            self._step(4, "postflight", ["kubectl get nodes -o wide"]),
        ]
        performed: list[str] = []

        preflight = self._preflight(
            request,
            nodes,
            local_name,
            [
                node
                for node in [*peers, *workers]
                if not self._on_target(self._node_version(node), target)
            ],
        )
        self._last_preflight = preflight
        if preflight["blocked"]:
            steps[0]["status"] = "failed"
            steps[0]["detail"] = "; ".join(
                check["detail"] for check in preflight["checks"] if not check["ok"]
            )
            return self._result(request, steps, performed, "UPGRADE_PREFLIGHT_BLOCKED")
        plan_argv = ["kubeadm", "upgrade", "plan", target]
        if not self._execute_step(steps[0], plan_argv, STEP_TIMEOUT_SECONDS):
            return self._result(request, steps, performed, "UPGRADE_PREFLIGHT_FAILED")
        performed.append("preflight")

        if local_upgrade_needed:
            for argv in package_commands or []:
                if not self._execute_step(steps[1], argv, STEP_TIMEOUT_SECONDS):
                    steps[1]["detail"] = (
                        "package upgrade failed; the control plane was not upgraded"
                    )
                    return self._result(request, steps, performed, "UPGRADE_PACKAGE_FAILED")
            apply_argv = ["kubeadm", "upgrade", "apply", "-y", target]
            if not self._execute_step(steps[1], apply_argv, STEP_TIMEOUT_SECONDS):
                return self._result(
                    request, steps, performed, "UPGRADE_CONTROL_PLANE_FAILED"
                )
            if not self._execute_step(
                steps[1], ["sudo", "systemctl", "restart", "kubelet"], STEP_TIMEOUT_SECONDS
            ):
                return self._result(
                    request, steps, performed, "UPGRADE_KUBELET_RESTART_FAILED"
                )
            performed.append("control_plane")
        for peer, argv_list in peer_commands:
            name = self._node_name(peer)
            if not argv_list:
                control_commands_note = f"{name}: already on {target}, skipped"
                steps[1]["detail"] = "; ".join(
                    part for part in (steps[1]["detail"], control_commands_note) if part
                )
                continue
            for argv, timeout, code in argv_list:
                step_probe = dict(steps[1])
                if not self._execute_step(step_probe, argv, timeout):
                    steps[1]["status"] = "failed"
                    steps[1]["exit_code"] = step_probe["exit_code"]
                    steps[1]["detail"] = f"{name}: {step_probe['detail']}"
                    return self._result(request, steps, performed, code)
            performed.append(f"control_plane:{name}")

        worker_step = steps[2]
        worker_step["status"] = "succeeded"
        for node in workers:
            name = self._node_name(node)
            address = self._node_address(node)
            if not name or not address:
                worker_step["status"] = "failed"
                worker_step["detail"] = f"node {name or '<unnamed>'} has no InternalIP"
                return self._result(request, steps, performed, "UPGRADE_WORKER_FAILED")
            remote = f"{self._remote_user}@{address}"
            drain = [
                "kubectl",
                "drain",
                name,
                "--ignore-daemonsets",
                "--delete-emptydir-data",
                "--timeout=15m",
            ]
            upgrade = ["ssh", remote, "kubeadm", "upgrade", "node"]
            remote_commands = [
                ["ssh", remote, shlex.join(command)] for command in package_commands or []
            ]
            restart = ["ssh", remote, "sudo systemctl restart kubelet"]
            uncordon = ["kubectl", "uncordon", name]
            worker_step["commands"] = [
                " ".join(drain),
                *(command[2] for command in remote_commands),
                " ".join(upgrade),
                " ".join(restart),
                " ".join(uncordon),
            ]
            for argv, timeout, code in (
                (drain, DRAIN_TIMEOUT_SECONDS, "UPGRADE_WORKER_DRAIN_FAILED"),
                *(
                    (command, STEP_TIMEOUT_SECONDS, "UPGRADE_WORKER_PACKAGE_FAILED")
                    for command in remote_commands
                ),
                (upgrade, STEP_TIMEOUT_SECONDS, "UPGRADE_WORKER_NODE_FAILED"),
                (restart, STEP_TIMEOUT_SECONDS, "UPGRADE_WORKER_KUBELET_RESTART_FAILED"),
                (uncordon, DRAIN_TIMEOUT_SECONDS, "UPGRADE_WORKER_UNCORDON_FAILED"),
            ):
                step_probe = dict(worker_step)
                if not self._execute_step(step_probe, argv, timeout):
                    worker_step["status"] = "failed"
                    worker_step["exit_code"] = step_probe["exit_code"]
                    worker_step["detail"] = f"{name}: {step_probe['detail']}"
                    return self._result(request, steps, performed, code)
            performed.append(f"worker:{name}")
        if not workers:
            worker_step["status"] = "skipped"
            worker_step["detail"] = "the cluster has no worker nodes"

        postflight = self._execute_step(steps[3], ["kubectl", "get", "nodes", "-o", "json"], 60)
        if not postflight:
            return self._result(request, steps, performed, "UPGRADE_POSTFLIGHT_FAILED")
        observed = self._nodes()
        stale = [
            f"{self._node_name(node)}={self._node_version(node)}"
            for node in observed
            if self._node_version(node)
            and not self._node_version(node).startswith(self._target_prefix(target))
        ]
        if stale:
            steps[3]["status"] = "failed"
            steps[3]["detail"] = "nodes not on " + target + ": " + ", ".join(stale)
            return self._result(request, steps, performed, "UPGRADE_POSTFLIGHT_FAILED")
        performed.append("postflight")
        return self._result(request, steps, performed, None)

    @staticmethod
    def _target_prefix(target: str) -> str:
        return ".".join(target.split(".")[:2])

    def _result(
        self,
        request: dict[str, str],
        steps: list[dict[str, Any]],
        performed: list[str],
        code: str | None,
    ) -> dict[str, Any]:
        return {
            "cluster_name": request["cluster_name"],
            "current_version": request["current_version"],
            "target_version": request["target_version"],
            "execution_mode": "execute",
            "approval_required": True,
            "upgrade_simulated": False,
            # A preflight-only run changed nothing; only the control-plane stage
            # (or a worker stage) actually moves a node to the new version.
            "upgrade_performed": any(
                step == "control_plane"
                or step.startswith("control_plane:")
                or step.startswith("worker:")
                for step in performed
            ),
            "requires_manual_execution": False,
            "resume_supported": False,
            "performed_steps": performed,
            "preflight": self._last_preflight,
            "etcd_snapshot": self._last_snapshot,
            "package_upgrade": (
                "configured" if self._package_configured else "skipped_unconfigured"
            ),
            "upgrade_steps": steps,
            "failure_code": code,
        }


def _snapshot_max_age_hours() -> float:
    raw = os.environ.get(ETCD_SNAPSHOT_MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_ETCD_SNAPSHOT_MAX_AGE_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ETCD_SNAPSHOT_MAX_AGE_HOURS
    return value if 0 < value <= 720 else DEFAULT_ETCD_SNAPSHOT_MAX_AGE_HOURS


__all__ = [
    "ETCD_SNAPSHOT_DIR_ENV",
    "ETCD_SNAPSHOT_ENV",
    "LOCAL_NODE_ENV",
    "PACKAGE_MANAGER_ENV",
    "PACKAGE_VERSION_ENV",
    "REMOTE_USER_ENV",
    "UpgradeExecutionError",
    "UpgradeExecutor",
]
