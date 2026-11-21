"""Kubernetes minor version upgrade request contract.

The platform used to validate this request inline and the Target Agent had no
handler at all, so a queued upgrade answered ``ACTION_NOT_IMPLEMENTED``. The
rules, the supported minor list and the simulated plan live here so the API and
the Agent cannot disagree about what a valid request is.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping
from typing import Any

SUPPORTED_MINORS: tuple[str, ...] = ("v1.33", "v1.34", "v1.35", "v1.36")
#: ``simulation`` only describes the plan; ``execute`` runs it on the cluster the
#: Target Agent belongs to. The platform requires an approval either way.
EXECUTION_MODES: tuple[str, ...] = ("simulation", "execute")

PAYLOAD_KEYS = frozenset(
    {"cluster_name", "current_version", "target_version", "execution_mode"}
)

_VERSION = re.compile(r"^v1\.\d+(?:\.\d+)?$")
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


#: Read-only probe that answers "is this Agent able to run an execute upgrade?"
READINESS_ACTION = "cluster.kubernetes.upgrade.readiness"
READINESS_PACKAGE_MANAGER_ENV = "KUBERNETES_UPGRADE_PACKAGE_MANAGER"
READINESS_PACKAGE_VERSION_ENV = "KUBERNETES_UPGRADE_PACKAGE_VERSION"
READINESS_REMOTE_USER_ENV = "KUBERNETES_UPGRADE_REMOTE_USER"
READINESS_PACKAGE_MANAGERS = ("apt", "dnf")
_PACKAGE_VERSION_SHAPE = re.compile(r"^[0-9A-Za-z.+~:-]{1,64}$")


class KubernetesUpgradeError(ValueError):
    """A request that cannot be turned into an upgrade plan."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _minor(version: str) -> int:
    return int(version.split(".")[1])


def minor_of(version: str) -> str:
    """Return ``v1.NN`` for any accepted version string."""

    return "v1." + version.split(".")[1]


def validate_request(
    payload: Any, *, cluster_name: str | None = None
) -> dict[str, str]:
    """Validate an upgrade request and return its normalized form.

    Only an adjacent minor upgrade is supported. Both execution modes are
    accepted here; what each one is allowed to do is decided by the caller.
    """

    if not isinstance(payload, dict):
        raise KubernetesUpgradeError("PAYLOAD_INVALID", "payload must be an object")
    if set(payload) != PAYLOAD_KEYS:
        raise KubernetesUpgradeError(
            "PAYLOAD_INVALID",
            "payload must contain exactly " + ", ".join(sorted(PAYLOAD_KEYS)),
        )
    name = payload.get("cluster_name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise KubernetesUpgradeError("CLUSTER_NAME_INVALID", "cluster_name is not a DNS name")
    if cluster_name is not None and name != cluster_name:
        raise KubernetesUpgradeError(
            "CLUSTER_MISMATCH", "cluster_name must match the Target Agent's cluster"
        )
    current = payload.get("current_version")
    target = payload.get("target_version")
    if not isinstance(current, str) or not _VERSION.match(current):
        raise KubernetesUpgradeError(
            "CURRENT_VERSION_INVALID", "current_version must look like v1.32.2"
        )
    if not isinstance(target, str) or not _VERSION.match(target):
        raise KubernetesUpgradeError(
            "TARGET_VERSION_INVALID", "target_version must look like v1.33.0"
        )
    target_minor = minor_of(target)
    if target_minor not in SUPPORTED_MINORS:
        raise KubernetesUpgradeError(
            "VERSION_UNSUPPORTED",
            f"target minor {target_minor} is not in " + ", ".join(SUPPORTED_MINORS),
        )
    if _minor(target) != _minor(current) + 1:
        raise KubernetesUpgradeError(
            "VERSION_STEP_INVALID",
            "only an upgrade to the next minor version is supported",
        )
    mode = payload.get("execution_mode")
    if mode not in EXECUTION_MODES:
        raise KubernetesUpgradeError(
            "EXECUTION_MODE_UNSUPPORTED",
            "execution_mode must be one of " + ", ".join(EXECUTION_MODES),
        )
    return {
        "cluster_name": name,
        "current_version": current,
        "target_version": target,
        "execution_mode": mode,
    }


def readiness_report(
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Report whether this host can run ``execution_mode=execute``.

    Only presence is reported, never a secret value. ``missing`` names the
    variables an operator still has to set, so the UI can show one actionable
    line instead of a bare "not ready".
    """

    env = os.environ if env is None else env
    which = shutil.which if which is None else which

    manager = (env.get(READINESS_PACKAGE_MANAGER_ENV) or "").strip()
    package_version = (env.get(READINESS_PACKAGE_VERSION_ENV) or "").strip()
    remote_user = (env.get(READINESS_REMOTE_USER_ENV) or "").strip()

    missing: list[str] = []
    detail: list[str] = []
    if not manager:
        missing.append(READINESS_PACKAGE_MANAGER_ENV)
    elif manager not in READINESS_PACKAGE_MANAGERS:
        detail.append(
            f"{READINESS_PACKAGE_MANAGER_ENV}={manager} is not one of "
            + ", ".join(READINESS_PACKAGE_MANAGERS)
        )
    if not package_version:
        missing.append(READINESS_PACKAGE_VERSION_ENV)
    elif not _PACKAGE_VERSION_SHAPE.match(package_version):
        detail.append(f"{READINESS_PACKAGE_VERSION_ENV} is not a package version")
    if not remote_user:
        missing.append(READINESS_REMOTE_USER_ENV)
        detail.append(
            "workers cannot be upgraded until a remote executor user is configured; "
            "a control-plane-only cluster does not need it"
        )

    binaries = {name: bool(which(name)) for name in ("kubeadm", "kubectl")}
    for name, present in binaries.items():
        if not present:
            detail.append(f"{name} is not on PATH for this Agent")

    package_configured = (
        manager in READINESS_PACKAGE_MANAGERS
        and bool(_PACKAGE_VERSION_SHAPE.match(package_version))
    )
    return {
        "action": READINESS_ACTION,
        "execution_mode_supported": ["simulation", "execute"],
        "execute_ready": package_configured and all(binaries.values()),
        "package_upgrade_configured": package_configured,
        "package_manager": manager if manager in READINESS_PACKAGE_MANAGERS else None,
        "remote_executor_configured": bool(remote_user),
        "binaries": binaries,
        "missing": missing,
        "detail": detail,
    }


def simulation_plan(request: dict[str, str]) -> dict[str, Any]:
    """Describe what a real upgrade would do, without touching the cluster.

    The shape is the one the platform's result validator accepts: four ordered
    stages that all remain unexecuted, and explicit simulation-only flags.
    """

    target = request["target_version"]
    return {
        "cluster_name": request["cluster_name"],
        "current_version": request["current_version"],
        "target_version": target,
        "approval_required": True,
        "upgrade_simulated": True,
        "upgrade_performed": False,
        "requires_manual_execution": True,
        "resume_supported": False,
        "upgrade_steps": [
            {
                "sequence": 1,
                "name": "preflight",
                "status": "not_run",
                "commands": [f"kubeadm upgrade plan {target}"],
            },
            {
                "sequence": 2,
                "name": "control_plane",
                "status": "not_started",
                "commands": [f"kubeadm upgrade apply {target}"],
            },
            {
                "sequence": 3,
                "name": "workers",
                "status": "not_started",
                "commands": [
                    "kubectl drain <worker> --ignore-daemonsets --delete-emptydir-data",
                    "upgrade kubeadm and kubelet packages, then kubeadm upgrade node",
                    "kubectl uncordon <worker>",
                ],
            },
            {
                "sequence": 4,
                "name": "postflight",
                "status": "not_started",
                "commands": ["kubectl get nodes -o wide", "verify every node reports " + target],
            },
        ],
    }
