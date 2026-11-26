"""Scale a CAPI MachineDeployment without recreating its worker templates.

The worker provisioning flow creates templates and a MachineDeployment. Day-2
scale-out only changes the replica count, so the controller keeps the same
KubeadmConfigTemplate, Metal3MachineTemplate and data template. Every mutation
is preceded by a read that proves the MachineDeployment belongs to this
cluster, deployment and target-agent.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from packages.agent_contracts.cluster_scale import (
    ACTION,
    ClusterScaleError,
    simulation_plan,
    validate_request,
)

MANAGED_BY_LABEL = "ai-agent-stack.io/managed-by"
DEPLOYMENT_ID_LABEL = "ai-agent-stack.io/deployment-id"
TARGET_CLUSTER_UID_ANNOTATION = "ai-agent-stack.io/target-cluster-uid"
CLUSTER_NAME_LABEL = "cluster.x-k8s.io/cluster-name"
RECONCILE_ATTEMPTS = 40
RECONCILE_INTERVAL_SECONDS = 3


class ClusterScaleExecutionError(ClusterScaleError):
    def __init__(self, code: str, detail: str, side_effects: bool = False):
        super().__init__(code, detail)
        self.side_effects = side_effects


def _run(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    *,
    lease_lost_event: Any = None,
    timeout: int = 60,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_COMMAND_LEASE_LOST", "The command lease was lost."
        )
    result = runner(arguments, timeout=timeout, max_output_bytes=4 * 1024 * 1024)
    if result.get("available") is True:
        return result
    error = result.get("error")
    if error == "COMMAND_LEASE_LOST":
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_COMMAND_LEASE_LOST", "The command lease was lost."
        )
    if result.get("returncode") == 404 or error == "NotFound":
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_RESOURCE_NOT_FOUND", "The requested Kubernetes resource was not found."
        )
    raise ClusterScaleExecutionError(
        "CLUSTER_SCALE_KUBECTL_FAILED",
        f"kubectl failed: {str(error or result.get('stderr') or 'unknown error')[:200]}",
    )


def _document(result: dict[str, Any], code: str) -> dict[str, Any]:
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise ClusterScaleExecutionError(code, "Kubernetes returned invalid JSON.") from exc
    if not isinstance(document, dict):
        raise ClusterScaleExecutionError(code, "Kubernetes returned an invalid document.")
    return document


def _cluster(runner, namespace: str, name: str, lease_lost_event) -> dict[str, Any]:
    result = _run(
        runner,
        ["get", "clusters.cluster.x-k8s.io", name, "--namespace", namespace, "-o", "json"],
        lease_lost_event=lease_lost_event,
    )
    document = _document(result, "CLUSTER_SCALE_TARGET_CLUSTER_JSON_INVALID")
    if not document.get("metadata", {}).get("uid"):
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_TARGET_CLUSTER_UID_REQUIRED", "The CAPI Cluster has no UID."
        )
    return document


def _machine_deployment(runner, namespace: str, name: str, lease_lost_event):
    result = _run(
        runner,
        ["get", "machinedeployments", name, "--namespace", namespace, "-o", "json"],
        lease_lost_event=lease_lost_event,
    )
    document = _document(result, "CLUSTER_SCALE_MACHINE_DEPLOYMENT_JSON_INVALID")
    metadata = document.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    return document, metadata, labels, annotations


def _discover_machine_deployment(runner, namespace: str, cluster_name: str, lease_lost_event):
    result = _run(
        runner,
        [
            "get",
            "machinedeployments",
            "--namespace",
            namespace,
            "-l",
            f"{CLUSTER_NAME_LABEL}={cluster_name}",
            "-o",
            "json",
        ],
        lease_lost_event=lease_lost_event,
    )
    document = _document(result, "CLUSTER_SCALE_MACHINE_DEPLOYMENT_JSON_INVALID")
    items = document.get("items")
    if not isinstance(items, list):
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_MACHINE_DEPLOYMENT_JSON_INVALID",
            "MachineDeployment list is malformed.",
        )
    if len(items) != 1:
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_MACHINE_DEPLOYMENT_AMBIGUOUS",
            f"Specify machine_deployment because the cluster has {len(items)} MachineDeployments.",
        )
    return items[0]


def _available_bmh_count(runner, namespace: str, lease_lost_event) -> int:
    result = _run(
        runner,
        ["get", "baremetalhosts.metal3.io", "--namespace", namespace, "-o", "json"],
        lease_lost_event=lease_lost_event,
    )
    document = _document(result, "CLUSTER_SCALE_BMH_INVENTORY_INVALID")
    hosts = document.get("items")
    if not isinstance(hosts, list):
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_BMH_INVENTORY_INVALID", "BareMetalHost list is malformed."
        )
    available = 0
    for host in hosts:
        if not isinstance(host, dict):
            continue
        spec = host.get("spec", {})
        status = host.get("status", {})
        provisioning = status.get("provisioning", {})
        if (
            spec.get("online") is not False
            and spec.get("consumerRef") is None
            and provisioning.get("state") in ("ready", "available")
            and status.get("operationalStatus") == "OK"
            and not status.get("errorMessage")
        ):
            available += 1
    return available


def _verify_ownership(cluster, labels, annotations, request):
    uid = cluster.get("metadata", {}).get("uid")
    if (
        labels.get(MANAGED_BY_LABEL) != "target-agent"
        or labels.get(CLUSTER_NAME_LABEL) != request["target_cluster_name"]
        or labels.get(DEPLOYMENT_ID_LABEL) in (None, "")
        or annotations.get(TARGET_CLUSTER_UID_ANNOTATION) != uid
    ):
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_MACHINE_DEPLOYMENT_NOT_OWNED",
            "The MachineDeployment is not owned by this cluster and target-agent.",
        )


def _replicas(document: dict[str, Any]) -> int:
    value = document.get("spec", {}).get("replicas")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_MACHINE_DEPLOYMENT_SPEC_INVALID",
            "MachineDeployment spec.replicas is missing.",
        )
    return value


def _wait_for_replicas(
    runner,
    namespace: str,
    name: str,
    desired: int,
    lease_lost_event,
) -> dict[str, Any]:
    observed: dict[str, Any] = {}
    for attempt in range(RECONCILE_ATTEMPTS):
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise ClusterScaleExecutionError(
                "CLUSTER_SCALE_COMMAND_LEASE_LOST",
                "The command lease was lost after the patch.",
                side_effects=True,
            )
        observed, metadata, _labels, _annotations = _machine_deployment(
            runner, namespace, name, lease_lost_event
        )
        spec_replicas = observed.get("spec", {}).get("replicas")
        status = observed.get("status", {})
        generation = metadata.get("generation", 0)
        observed_generation = status.get("observedGeneration")
        status_replicas = status.get("replicas")
        if (
            spec_replicas == desired
            and isinstance(observed_generation, int)
            and observed_generation >= generation
            and status_replicas == desired
        ):
            return observed
        if attempt < RECONCILE_ATTEMPTS - 1:
            time.sleep(RECONCILE_INTERVAL_SECONDS)
    raise ClusterScaleExecutionError(
        "CLUSTER_SCALE_REPLICAS_NOT_RECONCILED",
        "The MachineDeployment did not reconcile to the requested replica count.",
        side_effects=True,
    )


def execute_cluster_scale(
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
        request["machine_deployment"] = md.get("metadata", {}).get("name")
        _metadata, labels, annotations = (
            md.get("metadata", {}),
            md.get("metadata", {}).get("labels", {}),
            md.get("metadata", {}).get("annotations", {}),
        )
        if not isinstance(request["machine_deployment"], str) or not request["machine_deployment"]:
            raise ClusterScaleExecutionError(
                "CLUSTER_SCALE_MACHINE_DEPLOYMENT_JSON_INVALID",
                "The discovered MachineDeployment has no name.",
            )
    _verify_ownership(cluster, labels, annotations, request)
    current = _replicas(md)
    delta = request["desired_workers"] - current
    available = 0
    if delta > 0:
        available = _available_bmh_count(runner, namespace, lease_lost_event)
    plan = simulation_plan(request, current_workers=current, available_bmh=available)
    if request["execution_mode"] == "simulation":
        return plan
    if delta > 0 and available < delta:
        raise ClusterScaleExecutionError(
            "CLUSTER_SCALE_BMH_CAPACITY_INSUFFICIENT",
            f"Scale-out needs {delta} available BareMetalHosts but only {available} exist.",
        )
    if delta == 0:
        return {
            **plan,
            "execution_mode": "execute",
            "scale_simulated": False,
            "scale_performed": False,
            "side_effects": False,
        }
    result = _run(
        runner,
        [
            "patch",
            "machinedeployments",
            request["machine_deployment"],
            "--namespace",
            namespace,
            "--type=merge",
            "-p",
            json.dumps({"spec": {"replicas": request["desired_workers"]}}, separators=(",", ":")),
        ],
        lease_lost_event=lease_lost_event,
    )
    observed = _wait_for_replicas(
        runner,
        namespace,
        request["machine_deployment"],
        request["desired_workers"],
        lease_lost_event,
    )
    return {
        **plan,
        "execution_mode": "execute",
        "scale_simulated": False,
        "scale_performed": True,
        "side_effects": True,
        "previous_workers": current,
        "controller_replicas": _replicas(observed),
        "controller_status": {
            "replicas": observed.get("status", {}).get("replicas"),
            "updated_replicas": observed.get("status", {}).get("updatedReplicas"),
            "ready_replicas": observed.get("status", {}).get("readyReplicas"),
            "observed_generation": observed.get("status", {}).get("observedGeneration"),
        },
        "patch_stdout": result.get("stdout", "")[-2000:],
    }


__all__ = [
    "ACTION",
    "ClusterScaleExecutionError",
    "ClusterScaleError",
    "execute_cluster_scale",
]
