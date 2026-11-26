"""Day-2 repair through CAPI: delete one unhealthy worker Machine.

Deleting a Machine makes the MachineDeployment recreate that replica. The
BareMetalHost already owned by the unhealthy Machine is deprovisioned and
reclaimed, so repair replaces hardware rather than growing the fleet. This
module never edits a BareMetalHost directly and deletes at most one Machine per
approved command.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from packages.agent_contracts.cluster_repair import repair_plan, validate_request

from .cluster_scale import (
    CLUSTER_NAME_LABEL,
    ClusterScaleExecutionError,
    _cluster,
    _discover_machine_deployment,
    _document,
    _machine_deployment,
    _run,
    _verify_ownership,
)

REPAIR_GRACE_SECONDS = 600


class ClusterRepairExecutionError(ValueError):
    def __init__(self, code: str, detail: str, side_effects: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.side_effects = side_effects


def _creation_age_seconds(machine: dict[str, Any]) -> float | None:
    value = machine.get("metadata", {}).get("creationTimestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        created = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = dt.datetime.now(dt.UTC)
    return (now - created).total_seconds()


def _node_health(machine: dict[str, Any]) -> bool | None:
    conditions = machine.get("status", {}).get("conditions")
    if not isinstance(conditions, list):
        return None
    node_healthy = None
    ready = None
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        kind = condition.get("type")
        status = condition.get("status")
        if not isinstance(status, str):
            continue
        if kind == "NodeHealthy":
            node_healthy = status == "True"
        elif kind == "Ready":
            ready = status == "True"
    if node_healthy is not None:
        return node_healthy
    return ready


def _unhealthy(machine: dict[str, Any]) -> bool:
    if machine.get("metadata", {}).get("deletionTimestamp"):
        return False
    health = _node_health(machine)
    if health is not None:
        return not health
    age = _creation_age_seconds(machine)
    return age is not None and age >= REPAIR_GRACE_SECONDS


def _machines(runner, namespace: str, machine_deployment: str, cluster_name: str, lease_lost_event):
    result = _run(
        runner,
        [
            "get",
            "machines.cluster.x-k8s.io",
            "--namespace",
            namespace,
            "-l",
            f"{CLUSTER_NAME_LABEL}={cluster_name}",
            "-o",
            "json",
        ],
        lease_lost_event=lease_lost_event,
    )
    document = _document(result, "CLUSTER_REPAIR_MACHINES_JSON_INVALID")
    items = document.get("items")
    if not isinstance(items, list):
        raise ClusterRepairExecutionError(
            "CLUSTER_REPAIR_MACHINES_JSON_INVALID", "The Machine list is malformed."
        )
    selected = []
    for item in items:
        if not isinstance(item, dict):
            raise ClusterRepairExecutionError(
                "CLUSTER_REPAIR_MACHINES_JSON_INVALID", "A Machine entry is malformed."
            )
        labels = item.get("metadata", {}).get("labels")
        if not isinstance(labels, dict):
            continue
        if labels.get("cluster.x-k8s.io/deployment-name") != machine_deployment:
            continue
        selected.append(item)
    selected.sort(key=lambda item: str(item.get("metadata", {}).get("creationTimestamp", "")))
    return selected


def execute_cluster_repair(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    *,
    cluster_name: str | None = None,
) -> dict[str, Any]:
    request = validate_request(payload, cluster_name=cluster_name)
    namespace = request["target_namespace"]
    cluster = _cluster(runner, namespace, request["target_cluster_name"], lease_lost_event)
    if request["machine_deployment"]:
        md, _metadata, labels, annotations = _machine_deployment(
            runner, namespace, request["machine_deployment"], lease_lost_event
        )
    else:
        md = _discover_machine_deployment(
            runner, namespace, request["target_cluster_name"], lease_lost_event
        )
        _metadata, labels, annotations = (
            md.get("metadata", {}),
            md.get("metadata", {}).get("labels", {}),
            md.get("metadata", {}).get("annotations", {}),
        )
        request["machine_deployment"] = md.get("metadata", {}).get("name")
        if not isinstance(request["machine_deployment"], str) or not request["machine_deployment"]:
            raise ClusterRepairExecutionError(
                "CLUSTER_REPAIR_MACHINE_DEPLOYMENT_INVALID",
                "The discovered MachineDeployment has no name.",
            )
    try:
        _verify_ownership(cluster, labels, annotations, request)
    except ClusterScaleExecutionError as exc:
        raise ClusterRepairExecutionError(
            getattr(exc, "code", "CLUSTER_REPAIR_MACHINE_DEPLOYMENT_NOT_OWNED"),
            getattr(exc, "detail", str(exc) or "MachineDeployment ownership check failed."),
            getattr(exc, "side_effects", False),
        ) from exc

    machines = _machines(
        runner,
        namespace,
        request["machine_deployment"],
        request["target_cluster_name"],
        lease_lost_event,
    )
    unhealthy = [
        machine.get("metadata", {}).get("name") for machine in machines if _unhealthy(machine)
    ]
    unhealthy = [name for name in unhealthy if isinstance(name, str) and name]
    requested = request["machine_names"]
    if requested is not None:
        selected_unhealthy = [name for name in unhealthy if name in requested]
        known = {m.get("metadata", {}).get("name") for m in machines}
        missing = [name for name in requested if name not in known]
        if missing:
            raise ClusterRepairExecutionError(
                "CLUSTER_REPAIR_MACHINE_NOT_FOUND",
                "These Machines are not in the selected MachineDeployment: " + ", ".join(missing),
            )
        healthy_requested = [name for name in requested if name not in selected_unhealthy]
        if healthy_requested:
            raise ClusterRepairExecutionError(
                "CLUSTER_REPAIR_MACHINE_IS_HEALTHY",
                "Refusing to include a healthy Machine in a repair: "
                + ", ".join(healthy_requested),
            )
        unhealthy = selected_unhealthy
    plan = repair_plan(
        request,
        machine_deployment=request["machine_deployment"],
        total_machines=len(machines),
        healthy_machines=len(machines) - len(unhealthy),
        unhealthy_machines=unhealthy,
    )
    if request["execution_mode"] == "simulation":
        return plan
    if not unhealthy:
        return {
            **plan,
            "execution_mode": "execute",
            "repair_simulated": False,
            "repair_performed": False,
            "side_effects": False,
        }
    machine = unhealthy[0]
    _run(
        runner,
        [
            "delete",
            "machines.cluster.x-k8s.io",
            machine,
            "--namespace",
            namespace,
            "--ignore-not-found=false",
            "--wait=false",
        ],
        lease_lost_event=lease_lost_event,
        timeout=120,
    )
    return {
        **plan,
        "execution_mode": "execute",
        "repair_simulated": False,
        "repair_performed": True,
        "side_effects": True,
        "deleted_machines": [machine],
        "remaining_unhealthy_machines": unhealthy[1:],
        "replacement_expected": True,
    }
