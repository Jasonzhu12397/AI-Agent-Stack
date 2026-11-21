"""Kubernetes minor version upgrade request contract.

The platform used to validate this request inline and the Target Agent had no
handler at all, so a queued upgrade answered ``ACTION_NOT_IMPLEMENTED``. The
rules, the supported minor list and the simulated plan live here so the API and
the Agent cannot disagree about what a valid request is.
"""

from __future__ import annotations

import re
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
