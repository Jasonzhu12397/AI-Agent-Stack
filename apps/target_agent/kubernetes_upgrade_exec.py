"""Real execution of a Kubernetes minor upgrade on the Target Agent host.

The contract module decides whether a request is legal and describes what an
upgrade would do. This module is the only place that touches a live cluster, and
it is deliberately narrow:

* the Target Agent upgrades the control plane it is running beside
    (``kubeadm upgrade apply``);
* workers are upgraded through an explicitly configured remote executor
    (``KUBERNETES_UPGRADE_REMOTE_USER``); without it a cluster that has workers
    is refused instead of pretending workers were upgraded;
* multi control-plane clusters are refused: a rolling control-plane upgrade
    needs a coordination step no single agent can perform.

There is no rollback. A failed step stops the run, records the exact command and
its output, and leaves ``upgrade_performed`` true so the operator never reads a
half-upgraded cluster as untouched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable
from typing import Any

REMOTE_USER_ENV = "KUBERNETES_UPGRADE_REMOTE_USER"
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
        if len(control_planes) > 1:
            raise UpgradeExecutionError(
                "UPGRADE_MULTI_CONTROL_PLANE_UNSUPPORTED",
                "a rolling control-plane upgrade needs cluster-wide coordination",
            )
        if workers and not self._remote_user:
            raise UpgradeExecutionError(
                "UPGRADE_REMOTE_EXECUTOR_NOT_CONFIGURED",
                f"set {REMOTE_USER_ENV} to upgrade worker nodes from this host",
            )

        package_commands = self._package_commands(target)
        control_commands = [
            *(shlex.join(command) for command in package_commands or []),
            f"kubeadm upgrade apply -y {target}",
            "sudo systemctl restart kubelet",
        ]
        steps = [
            self._step(1, "preflight", [f"kubeadm upgrade plan {target}"]),
            self._step(2, "control_plane", control_commands),
            self._step(3, "workers", []),
            self._step(4, "postflight", ["kubectl get nodes -o wide"]),
        ]
        performed: list[str] = []

        plan_argv = ["kubeadm", "upgrade", "plan", target]
        if not self._execute_step(steps[0], plan_argv, STEP_TIMEOUT_SECONDS):
            return self._result(request, steps, performed, "UPGRADE_PREFLIGHT_FAILED")
        performed.append("preflight")

        for argv in package_commands or []:
            if not self._execute_step(steps[1], argv, STEP_TIMEOUT_SECONDS):
                steps[1]["detail"] = (
                    "package upgrade failed; the control plane was not upgraded"
                )
                return self._result(request, steps, performed, "UPGRADE_PACKAGE_FAILED")
        apply_argv = ["kubeadm", "upgrade", "apply", "-y", target]
        if not self._execute_step(steps[1], apply_argv, STEP_TIMEOUT_SECONDS):
            return self._result(request, steps, performed, "UPGRADE_CONTROL_PLANE_FAILED")
        if not self._execute_step(
            steps[1], ["sudo", "systemctl", "restart", "kubelet"], STEP_TIMEOUT_SECONDS
        ):
            return self._result(request, steps, performed, "UPGRADE_KUBELET_RESTART_FAILED")
        performed.append("control_plane")

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
            "upgrade_performed": "control_plane" in performed
            or any(step.startswith("worker:") for step in performed),
            "requires_manual_execution": False,
            "resume_supported": False,
            "performed_steps": performed,
            "package_upgrade": (
                "configured" if self._package_configured else "skipped_unconfigured"
            ),
            "upgrade_steps": steps,
            "failure_code": code,
        }


__all__ = [
    "PACKAGE_MANAGER_ENV",
    "PACKAGE_VERSION_ENV",
    "REMOTE_USER_ENV",
    "UpgradeExecutionError",
    "UpgradeExecutor",
]
