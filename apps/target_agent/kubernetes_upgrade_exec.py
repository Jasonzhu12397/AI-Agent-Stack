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
ETCD_HEALTH_ATTEMPTS = 20
ETCD_HEALTH_INTERVAL_SECONDS = 5.0
STEP_TIMEOUT_SECONDS = 1800
DRAIN_TIMEOUT_SECONDS = 900

RunCommand = Callable[..., dict[str, Any]]


def _snapshot_path_problem(path: str) -> str:
    from packages.agent_contracts.kubernetes_rollback import snapshot_pod_path_problem

    return snapshot_pod_path_problem(path)


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

    def __init__(
        self,
        runner: RunCommand,
        *,
        remote_user: str | None = None,
        etcd_health_attempts: int = ETCD_HEALTH_ATTEMPTS,
        etcd_health_interval: float = ETCD_HEALTH_INTERVAL_SECONDS,
    ) -> None:
        self._runner = runner
        self._remote_user = (remote_user if remote_user is not None else
                             os.environ.get(REMOTE_USER_ENV, "")).strip()
        self._package_configured = bool(
            os.environ.get(PACKAGE_MANAGER_ENV, "").strip()
        )
        self._last_preflight: dict[str, Any] = {"checks": [], "blocked": False}
        self._last_snapshot: dict[str, Any] | None = None
        self._etcd_pod = ""
        self._already_at_target = False
        self._etcd_health: list[dict[str, Any]] = []
        self._etcd_health_attempts = max(1, int(etcd_health_attempts))
        self._etcd_health_interval = max(0.0, float(etcd_health_interval))

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
    def _version_triplet(version: str) -> tuple[int, int, int] | None:
        match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
        if not match:
            return None
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]

    @classmethod
    def _on_target(cls, version: str, target: str) -> bool:
        """Whether a node already runs at least the requested version.

        Comparing minors only treated ``v1.34.0`` as done when the request
        asked for ``v1.34.3``: a patch release an operator explicitly named was
        never installed and the run reported success. The whole triplet is
        compared, so a node is skipped only when it is at or above the target.
        """

        node = cls._version_triplet(version)
        wanted = cls._version_triplet(target)
        if node is None or wanted is None:
            return False
        return node >= wanted

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

    def _etcdctl_argv(self, pod: str) -> list[str]:
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
        self, pod: str, local_node: str, target: str, cluster_name: str = ""
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
        # The cluster name belongs in the file name: the backup directory is
        # pruned by the recurring backup job, and a file it cannot attribute to
        # this cluster would either be deleted by mistake or never deleted at
        # all. The target version stays so an operator can see what the
        # snapshot protects.
        owner = f"{cluster_name}-{target}" if cluster_name else target
        name = f"snapshot-pre-{owner}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
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
            [*self._etcdctl_argv(pod), "snapshot", "save", path],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not save.get("available", True) or save.get("returncode") != 0:
            return None, self._check(
                "etcd.snapshot",
                False,
                f"snapshot save failed on {local_node}: " + (_output(save) or "no output"),
            )
        status = self._runner(
            [*self._etcdctl_argv(pod), "snapshot", "status", path],
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

    def _etcd_quorum_healthy(self, pod: str) -> tuple[bool, str]:
        """One non-waiting read of cluster-wide etcd health."""

        result = self._runner(
            [*self._etcdctl_argv(pod), "endpoint", "health", "--cluster"],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        ok = result.get("available", True) and result.get("returncode") == 0
        return ok, _output(result) or ("healthy" if ok else "unhealthy")

    def _wait_etcd_healthy(self, pod: str, stage: str) -> dict[str, Any]:
        """Wait for etcd to be healthy again after a member was upgraded.

        A rolling upgrade restarts etcd members one at a time. If the cluster
        does not come back healthy within the window, continuing would push it
        below quorum, so the run stops instead of upgrading the next node.
        """

        detail = ""
        for attempt in range(self._etcd_health_attempts):
            ok, detail = self._etcd_quorum_healthy(pod)
            if ok:
                return self._check(
                    f"etcd.quorum/{stage}",
                    True,
                    f"healthy after {attempt + 1} probe(s)",
                )
            if attempt + 1 < self._etcd_health_attempts:
                time.sleep(self._etcd_health_interval)
        return self._check(
            f"etcd.quorum/{stage}",
            False,
            f"etcd did not become healthy: {detail}",
        )

    def _guard_etcd(self, step: dict[str, Any], stage: str) -> str | None:
        """Stop the rolling upgrade when etcd does not come back healthy."""

        if not self._etcd_pod:
            return None
        check = self._wait_etcd_healthy(self._etcd_pod, stage)
        self._etcd_health.append(check)
        if check["ok"]:
            return None
        step["status"] = "failed"
        step["detail"] = f"{stage}: {check['detail']}"
        return "UPGRADE_ETCD_QUORUM_AT_RISK"

    def _etcd_quorum_checks(self) -> list[dict[str, Any]]:
        if not self._etcd_pod:
            return []
        ok, detail = self._etcd_quorum_healthy(self._etcd_pod)
        return [
            self._check(
                "etcd.quorum",
                ok,
                detail if ok else f"etcd is not healthy before the upgrade: {detail}",
            )
        ]

    @staticmethod
    def _snapshot_pod(pods: list[dict[str, Any]], local_node: str) -> str:
        """The etcd pod on the local control plane, else the first one."""

        for item in pods:
            if str((item.get("spec") or {}).get("nodeName")) == local_node:
                name = str((item.get("metadata") or {}).get("name") or "")
                if name:
                    return name
        return str((pods[0].get("metadata") or {}).get("name") or "")

    def _etcd_snapshot_checks(
        self, local_node: str, target: str, cluster_name: str = ""
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
            problem = _snapshot_path_problem(supplied)
            if problem:
                return [self._check("etcd.snapshot", False, problem)]
            max_age = _snapshot_max_age_hours()
            taken = age_hours <= max_age
            # A file that exists is not a snapshot that can be restored: the
            # restore reads it inside the etcd pod, so verify it there.
            pod = self._snapshot_pod(pods, local_node)
            verified = False
            detail = "no etcd pod to verify the snapshot in"
            if pod and taken:
                probe = self._runner(
                    [
                        *self._etcdctl_argv(pod),
                        "snapshot",
                        "status",
                        supplied,
                        "--write-out=json",
                    ],
                    timeout=60,
                )
                body = (probe.get("stdout") or "").strip()
                verified = (
                    probe.get("available", True)
                    and probe.get("returncode") == 0
                    and body.startswith("{")
                    and "hash" in body
                )
                detail = (
                    "snapshot status verified it"
                    if verified
                    else f"snapshot status could not verify it: {_output(probe)}"
                )
            if taken and verified:
                self._last_snapshot = {
                    "path": supplied,
                    "verified": True,
                    "taken": False,
                }
            return [
                self._check(
                    "etcd.snapshot",
                    taken and verified,
                    f"reusing {supplied} ({age_hours:.1f}h old, limit {max_age}h): {detail}",
                )
            ]
        pod = self._snapshot_pod(pods, local_node)
        if not pod:
            return [
                self._check("etcd.snapshot", False, "no etcd pod name to run etcdctl in")
            ]
        self._etcd_pod = pod
        snapshot, check = self._take_etcd_snapshot(
            pod, local_node, target, cluster_name
        )
        self._last_snapshot = snapshot
        return [check, *self._etcd_quorum_checks()]

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
            if not ok:
                continue
            # Every remote step runs unattended under sudo; a sudoers entry that
            # asks for a password fails at once over BatchMode and would strand a
            # half-upgraded cluster, so it is a preflight fact too.
            sudo = self._runner(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    f"{self._remote_user}@{address}",
                    "sudo",
                    "-n",
                    "true",
                ],
                timeout=30,
            )
            sudo_ok = sudo.get("available", True) and sudo.get("returncode") == 0
            checks.append(
                self._check(
                    f"ssh.sudo/{name}",
                    sudo_ok,
                    "passwordless sudo works over ssh"
                    if sudo_ok
                    else "remote sudo needs a password; add a NOPASSWD entry for "
                    f"{self._remote_user}: {_output(sudo)}",
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
            *self._etcd_snapshot_checks(
                local_node, request["target_version"], request["cluster_name"]
            ),
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
        self._already_at_target = all(
            self._on_target(self._node_version(node), target) for node in nodes
        )
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
        if not local_upgrade_needed and not any(
            argv_list for _peer, argv_list in peer_commands
        ):
            steps[1]["status"] = "skipped"
            steps[1]["detail"] = f"every control plane already runs {target}"
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
            code = self._guard_etcd(steps[1], local_name)
            if code:
                return self._result(request, steps, performed, code)
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
            code = self._guard_etcd(steps[1], name)
            if code:
                return self._result(request, steps, performed, code)
            performed.append(f"control_plane:{name}")

        worker_step = steps[2]
        worker_step["status"] = "succeeded"
        for node in workers:
            name = self._node_name(node)
            address = self._node_address(node)
            if self._on_target(self._node_version(node), target):
                note = f"{name}: already on {target}, skipped"
                worker_step["detail"] = "; ".join(
                    part for part in (worker_step["detail"], note) if part
                )
                continue
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
        elif not [item for item in performed if item.startswith("worker:")]:
            worker_step["status"] = "skipped"
            worker_step["detail"] = "; ".join(
                part
                for part in (worker_step["detail"], f"every worker already on {target}")
                if part
            )

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

    def report_recovery(self, target: str = "") -> dict[str, Any]:
        """Read-only state of a cluster that may or may not have upgraded.

        Nothing here mutates: it lists every node with the version it actually
        reports, whether that matches the requested target, the health of the
        stacked etcd members and the snapshots the upgrade path already wrote.
        """

        snapshot_dir = os.environ.get(ETCD_SNAPSHOT_DIR_ENV, "").strip() or (
            DEFAULT_ETCD_SNAPSHOT_DIR
        )
        try:
            nodes = self._nodes()
        except UpgradeExecutionError as error:
            raise UpgradeExecutionError(
                "RECOVERY_NODES_UNREADABLE", error.detail
            ) from error
        node_rows: list[dict[str, Any]] = []
        for node in nodes:
            version = self._node_version(node)
            node_rows.append(
                {
                    "name": self._node_name(node),
                    "role": "control-plane" if self._is_control_plane(node) else "worker",
                    "version": version,
                    "on_target": bool(target) and self._on_target(version, target),
                }
            )
        warnings: list[str] = []
        etcd: dict[str, Any] = {"available": False, "healthy": False, "members": []}
        try:
            pods = self._etcd_pods()
        except UpgradeExecutionError as error:
            etcd["error"] = error.code
            warnings.append(f"etcd state is unreadable: {error.detail}")
        else:
            etcd["available"] = True
            members = []
            for item in pods:
                metadata = item.get("metadata") or {}
                name = str(metadata.get("name") or "")
                if not name:
                    continue
                ok, detail = self._etcd_quorum_healthy(name)
                members.append(
                    {
                        "pod": name,
                        "node": str((item.get("spec") or {}).get("nodeName") or ""),
                        "healthy": ok,
                        "detail": detail,
                    }
                )
            etcd["members"] = members
            etcd["stacked"] = bool(members)
            # No stacked etcd (external etcd) is not a degraded cluster; saying
            # so would send an operator looking for members that never existed.
            etcd["healthy"] = (
                all(member["healthy"] for member in members) if members else None
            )
            if members and not etcd["healthy"]:
                warnings.append("at least one etcd member is not healthy")
        snapshots: dict[str, Any] = {"directory": snapshot_dir, "entries": []}
        listing = self._runner(
            [
                "sudo",
                "find",
                snapshot_dir,
                "-maxdepth",
                "1",
                "-name",
                "*.db",
                "-printf",
                "%f %s %TY-%Tm-%TdT%TH:%TM:%TS\n",
            ],
            timeout=60,
        )
        if not listing.get("available", True) or listing.get("returncode") != 0:
            snapshots["error"] = _output(listing) or "snapshot directory is unreadable"
            warnings.append(f"{snapshot_dir} is unreadable")
        else:
            for line in (listing.get("stdout") or "").splitlines():
                parts = line.split()
                if len(parts) != 3:
                    continue
                name, size, taken = parts
                snapshots["entries"].append(
                    {"name": name, "bytes": int(size) if size.isdigit() else 0,
                     "taken": taken}
                )
        return {
            "target_version": target,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nodes": node_rows,
            # Without a target there is nothing to drift from; zero would read
            # as "all nodes are fine".
            "drift": sum(1 for row in node_rows if not row["on_target"]) if target else None,
            "etcd": etcd,
            "snapshots": snapshots,
            "warnings": warnings[:10],
        }

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
            # A retry of an upgrade that already finished changes nothing. The
            # platform needs that stated, or it cannot tell a no-op retry from a
            # run that quietly failed to upgrade anything.
            "already_at_target": self._already_at_target,
            "requires_manual_execution": False,
            "resume_supported": False,
            "performed_steps": performed,
            "preflight": self._last_preflight,
            "etcd_snapshot": self._last_snapshot,
            "etcd_health": self._etcd_health,
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
