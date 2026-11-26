"""Validation for changing a CAPI MachineDeployment's worker replica count.

Scale-out is a real mutation: the controller turns the requested replicas into
new Metal3 machines, so the request is validated before dispatch and the
executor re-checks cluster ownership before patching.
"""

from __future__ import annotations

import re
from typing import Any

ACTION = "cluster.scale"
MAX_WORKERS = 2500
NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
EXECUTION_MODES = ("simulation", "execute")


class ClusterScaleError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 63:
        raise ClusterScaleError("CLUSTER_SCALE_REQUEST_INVALID", f"{field} is required.")
    if not NAME_PATTERN.fullmatch(value):
        raise ClusterScaleError(
            "CLUSTER_SCALE_REQUEST_INVALID", f"{field} must be a Kubernetes DNS label."
        )
    return value


def validate_request(payload: dict[str, Any], cluster_name: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ClusterScaleError("CLUSTER_SCALE_REQUEST_INVALID", "Payload must be an object.")
    target = _name(payload.get("target_cluster_name"), "target_cluster_name")
    if cluster_name and target != cluster_name:
        raise ClusterScaleError(
            "CLUSTER_SCALE_CLUSTER_MISMATCH",
            "The requested cluster is not the cluster this Agent is enrolled for.",
        )
    raw_md = payload.get("machine_deployment")
    machine_deployment = _name(raw_md, "machine_deployment") if raw_md else None
    namespace = _name(payload.get("target_namespace") or "default", "target_namespace")
    desired = payload.get("desired_workers")
    if isinstance(desired, bool) or not isinstance(desired, int) or desired < 1:
        raise ClusterScaleError(
            "CLUSTER_SCALE_REQUEST_INVALID", "desired_workers must be a positive integer."
        )
    if desired > MAX_WORKERS:
        raise ClusterScaleError(
            "CLUSTER_SCALE_REQUEST_INVALID", f"desired_workers cannot exceed {MAX_WORKERS}."
        )
    mode = payload.get("execution_mode") or "simulation"
    if mode not in EXECUTION_MODES:
        raise ClusterScaleError(
            "CLUSTER_SCALE_REQUEST_INVALID", "execution_mode must be simulation or execute."
        )
    reason = payload.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 300:
        raise ClusterScaleError("CLUSTER_SCALE_REQUEST_INVALID", "reason is too long.")
    return {
        "target_cluster_name": target,
        "machine_deployment": machine_deployment,
        "target_namespace": namespace,
        "desired_workers": desired,
        "execution_mode": mode,
        "reason": reason,
    }


def simulation_plan(request: dict[str, Any], *, current_workers: int, available_bmh: int) -> dict:
    delta = request["desired_workers"] - current_workers
    capacity_ok = delta <= 0 or available_bmh >= delta
    return {
        "action": ACTION,
        "cluster_name": request["target_cluster_name"],
        "machine_deployment": request["machine_deployment"],
        "namespace": request["target_namespace"],
        "current_workers": current_workers,
        "desired_workers": request["desired_workers"],
        "worker_delta": delta,
        "available_bmh_count": available_bmh,
        "bmh_capacity_sufficient": capacity_ok,
        "execution_mode": request["execution_mode"],
        "approval_required": True,
        "scale_simulated": True,
        "scale_performed": False,
        "side_effects": False,
    }
