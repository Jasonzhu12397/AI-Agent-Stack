"""Day-2 worker repair request contract.

Repair means replacing exactly one unhealthy worker Machine through CAPI. The
platform and the Agent share these rules so a command cannot delete a Machine
outside the enrolled cluster or outside a target-owned MachineDeployment.
"""

from __future__ import annotations

import re
from typing import Any

ACTION = "cluster.worker.repair"
EXECUTION_MODES = ("simulation", "execute")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_VERSION = re.compile(r"v1\.[0-9]+")


class ClusterRepairError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 63 or not _NAME.fullmatch(value):
        raise ClusterRepairError(
            "CLUSTER_REPAIR_REQUEST_INVALID", f"{field} must be a Kubernetes name."
        )
    return value


def validate_request(payload: Any, *, cluster_name: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ClusterRepairError("CLUSTER_REPAIR_REQUEST_INVALID", "payload must be an object.")
    target = _name(payload.get("target_cluster_name"), "target_cluster_name")
    if cluster_name and target != cluster_name:
        raise ClusterRepairError(
            "CLUSTER_REPAIR_CLUSTER_NOT_ENROLLED",
            "The request targets a cluster this Agent is not enrolled for.",
        )
    namespace = _name(payload.get("target_namespace") or "default", "target_namespace")
    raw_md = payload.get("machine_deployment")
    machine_deployment = _name(raw_md, "machine_deployment") if raw_md else None
    mode = payload.get("execution_mode")
    if mode not in EXECUTION_MODES:
        raise ClusterRepairError(
            "CLUSTER_REPAIR_REQUEST_INVALID", "execution_mode must be simulation or execute."
        )
    reason = payload.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 300:
        raise ClusterRepairError("CLUSTER_REPAIR_REQUEST_INVALID", "reason is too long.")
    candidates = payload.get("machine_names")
    if candidates is not None:
        if not isinstance(candidates, list) or not candidates:
            raise ClusterRepairError(
                "CLUSTER_REPAIR_REQUEST_INVALID", "machine_names must be a non-empty list."
            )
        machine_names = [_name(value, "machine_names") for value in candidates]
    else:
        machine_names = None
    return {
        "target_cluster_name": target,
        "target_namespace": namespace,
        "machine_deployment": machine_deployment,
        "execution_mode": mode,
        "reason": reason,
        "machine_names": machine_names,
    }


def repair_plan(
    request: dict[str, Any],
    *,
    machine_deployment: str,
    total_machines: int,
    healthy_machines: int,
    unhealthy_machines: list[str],
) -> dict[str, Any]:
    selected = unhealthy_machines[0] if unhealthy_machines else None
    return {
        "action": ACTION,
        "cluster_name": request["target_cluster_name"],
        "namespace": request["target_namespace"],
        "machine_deployment": machine_deployment,
        "total_machines": total_machines,
        "healthy_machines": healthy_machines,
        "unhealthy_machines": unhealthy_machines,
        "selected_machine": selected,
        "reclaims_existing_baremetalhost": True,
        "execution_mode": request["execution_mode"],
        "approval_required": True,
        "repair_simulated": True,
        "repair_performed": False,
        "side_effects": False,
    }
