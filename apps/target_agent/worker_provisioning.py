from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from threading import Event
from typing import Any

from .ntp_policy import (
    NtpPolicyError,
    render_worker_chrony_pre_kubeadm_command,
    validate_ironic_internal_ntp_ip,
)

RESOURCE_TYPES = {
    "KubeadmConfigTemplate": (
        "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
        "bootstrap.cluster.x-k8s.io/v1beta1",
    ),
    "Metal3MachineTemplate": (
        "metal3machinetemplates.infrastructure.cluster.x-k8s.io",
        "infrastructure.cluster.x-k8s.io/v1beta1",
    ),
    "MachineDeployment": (
        "machinedeployments.cluster.x-k8s.io",
        "cluster.x-k8s.io/v1beta1",
    ),
}
NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
NTP_DIRECTIVE_PATTERN = re.compile(r"(?im)^\s*(?:server|pool)\s+")


class WorkerProvisioningError(RuntimeError):
    pass


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise WorkerProvisioningError(f"WORKER_{field}_INVALID")
    return value


def _run_kubectl(
    arguments: list[str],
    input_text: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    allowed = (
        "PATH", "HOME", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "XDG_CACHE_HOME",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    if os.environ.get("KUBECONFIG"):
        environment["KUBECONFIG"] = os.environ["KUBECONFIG"]
    try:
        result = subprocess.run(
            ["kubectl", *arguments],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerProvisioningError(f"WORKER_KUBECTL_{type(exc).__name__.upper()}") from exc
    if len(result.stdout.encode("utf-8")) > 8 * 1024 * 1024:
        raise WorkerProvisioningError("WORKER_KUBECTL_OUTPUT_LIMIT_EXCEEDED")
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr[-2000:],
    }


def _object_identity(document: dict[str, Any]) -> tuple[str, str, str]:
    kind = document.get("kind")
    if kind not in RESOURCE_TYPES:
        raise WorkerProvisioningError("WORKER_RESOURCE_KIND_NOT_ALLOWED")
    api_version = document.get("apiVersion")
    _expected_resource, expected_api_version = RESOURCE_TYPES[kind]
    if api_version != expected_api_version:
        raise WorkerProvisioningError(f"WORKER_{kind.upper()}_API_VERSION_UNSUPPORTED")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise WorkerProvisioningError(f"WORKER_{kind.upper()}_METADATA_REQUIRED")
    namespace = _name(metadata.get("namespace"), "NAMESPACE")
    name = _name(metadata.get("name"), "RESOURCE_NAME")
    return expected_resource, namespace, name


def _selector_labels(machine_deployment: dict[str, Any]) -> dict[str, str]:
    spec = machine_deployment.get("spec")
    if not isinstance(spec, dict):
        raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_SPEC_REQUIRED")
    selector_spec = spec.get("selector")
    template_spec = spec.get("template")
    if not isinstance(selector_spec, dict) or not isinstance(template_spec, dict):
        raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_SELECTOR_INVALID")
    selector = selector_spec.get("matchLabels")
    template_labels = template_spec.get("metadata", {}).get("labels")
    if (
        not isinstance(selector, dict)
        or not selector
        or not isinstance(template_labels, dict)
        or not selector.items() <= template_labels.items()
    ):
        raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_SELECTOR_INVALID")
    for key, value in selector.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_SELECTOR_INVALID")
    return selector


def _prepare_resources(
    payload: dict[str, Any],
    ironic_internal_ntp_ip: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], int, str, str]:
    namespace = _name(payload.get("namespace"), "NAMESPACE")
    desired = payload.get("desired_worker_count")
    if not isinstance(desired, int) or isinstance(desired, bool) or not 1 <= desired <= 500:
        raise WorkerProvisioningError("WORKER_DESIRED_COUNT_INVALID")
    supplied_ntp = payload.get("ironic_internal_ntp_ip")
    try:
        ntp_ip = validate_ironic_internal_ntp_ip(str(supplied_ntp or ""))
    except NtpPolicyError as exc:
        raise WorkerProvisioningError("WORKER_IRONIC_INTERNAL_NTP_IP_INVALID") from exc
    if ntp_ip != ironic_internal_ntp_ip:
        raise WorkerProvisioningError("WORKER_IRONIC_INTERNAL_NTP_IP_CONFIGURATION_MISMATCH")
    raw_resources = payload.get("resources")
    if not isinstance(raw_resources, list) or len(raw_resources) != 3:
        raise WorkerProvisioningError("WORKER_RESOURCE_BUNDLE_MUST_CONTAIN_THREE_OBJECTS")
    resources_by_kind: dict[str, dict[str, Any]] = {}
    for raw in raw_resources:
        if not isinstance(raw, dict):
            raise WorkerProvisioningError("WORKER_RESOURCE_OBJECT_INVALID")
        document = copy.deepcopy(raw)
        kind = document.get("kind")
        if kind not in RESOURCE_TYPES or kind in resources_by_kind:
            raise WorkerProvisioningError("WORKER_RESOURCE_KIND_SET_INVALID")
        _object_identity(document)
        if document["metadata"]["namespace"] != namespace:
            raise WorkerProvisioningError("WORKER_RESOURCE_NAMESPACE_MISMATCH")
        resources_by_kind[kind] = document
    if set(resources_by_kind) != set(RESOURCE_TYPES):
        raise WorkerProvisioningError("WORKER_RESOURCE_KIND_SET_INCOMPLETE")
    machine_deployment = resources_by_kind["MachineDeployment"]
    bootstrap_template = resources_by_kind["KubeadmConfigTemplate"]
    infrastructure_template = resources_by_kind["Metal3MachineTemplate"]
    md_spec = machine_deployment.get("spec")
    if not isinstance(md_spec, dict) or md_spec.get("replicas") != desired:
        raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_REPLICA_MISMATCH")
    selector = _selector_labels(machine_deployment)
    template_spec = md_spec.get("template", {}).get("spec", {})
    bootstrap = template_spec.get("bootstrap")
    bootstrap_ref = bootstrap.get("configRef") if isinstance(bootstrap, dict) else None
    infrastructure_ref = template_spec.get("infrastructureRef")
    for reference, kind, document in (
        (bootstrap_ref, "KubeadmConfigTemplate", bootstrap_template),
        (infrastructure_ref, "Metal3MachineTemplate", infrastructure_template),
    ):
        if (
            not isinstance(reference, dict)
            or reference.get("kind") != kind
            or reference.get("name") != document["metadata"]["name"]
            or reference.get("namespace", namespace) != namespace
            or reference.get("apiVersion") != document["apiVersion"]
        ):
            raise WorkerProvisioningError(f"WORKER_{kind.upper()}_REFERENCE_MISMATCH")
    bootstrap_spec = bootstrap_template.get("spec", {}).get("template", {}).get("spec")
    if not isinstance(bootstrap_spec, dict):
        raise WorkerProvisioningError("WORKER_KUBEADM_CONFIG_TEMPLATE_SPEC_REQUIRED")
    commands = bootstrap_spec.get("preKubeadmCommands", [])
    if not isinstance(commands, list) or any(not isinstance(item, str) for item in commands):
        raise WorkerProvisioningError("WORKER_PRE_KUBEADM_COMMANDS_INVALID")
    if any(NTP_DIRECTIVE_PATTERN.search(command) for command in commands):
        raise WorkerProvisioningError("WORKER_EXTERNAL_OR_DUPLICATE_NTP_DIRECTIVE_REJECTED")
    ntp_command = render_worker_chrony_pre_kubeadm_command(ntp_ip)
    bootstrap_spec["preKubeadmCommands"] = [*commands, ntp_command]
    return (
        [
            bootstrap_template,
            infrastructure_template,
            machine_deployment,
        ],
        selector,
        desired,
        namespace,
        ntp_ip,
    )


def _check_lease(lease_lost_event: Event | None) -> None:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise WorkerProvisioningError("WORKER_COMMAND_LEASE_LOST")


def _get_resource(
    runner: Callable[..., dict[str, Any]],
    resource: str,
    name: str,
    namespace: str,
) -> dict[str, Any] | None:
    result = runner([
        "get", resource, name, "--namespace", namespace, "-o", "json",
    ])
    if not result["ok"]:
        error = result.get("stderr", "").lower()
        if "notfound" in error or "not found" in error:
            return None
        raise WorkerProvisioningError(f"WORKER_RESOURCE_READ_FAILED:{resource}")
    try:
        document = json.loads(result["stdout"])
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError(f"WORKER_RESOURCE_JSON_INVALID:{resource}") from exc
    if not isinstance(document, dict):
        raise WorkerProvisioningError(f"WORKER_RESOURCE_OBJECT_INVALID:{resource}")
    return document


def _create_immutable_template(
    document: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
) -> None:
    resource, namespace, name = _object_identity(document)
    current = _get_resource(runner, resource, name, namespace)
    if current is not None:
        if current.get("spec") != document.get("spec"):
            raise WorkerProvisioningError(f"WORKER_IMMUTABLE_TEMPLATE_CONFLICT:{name}")
        return
    result = runner(
        ["create", "-f", "-"],
        input_text=json.dumps(document, sort_keys=True),
    )
    if not result["ok"]:
        raise WorkerProvisioningError(f"WORKER_TEMPLATE_CREATE_FAILED:{name}")


def _apply_machine_deployment(
    document: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
) -> None:
    result = runner(
        ["apply", "--server-side", "--field-manager=target-agent", "-f", "-"],
        input_text=json.dumps(document, sort_keys=True),
    )
    if not result["ok"]:
        raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_APPLY_FAILED")


def _is_ready(node: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in node.get("status", {}).get("conditions", [])
    )


def execute_worker_provisioning(
    payload: dict[str, Any],
    lease_lost_event: Event | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WorkerProvisioningError("WORKER_PAYLOAD_OBJECT_REQUIRED")
    configured_ntp = os.environ.get("IRONIC_INTERNAL_NTP_IP", "")
    try:
        configured_ntp = validate_ironic_internal_ntp_ip(configured_ntp)
    except NtpPolicyError as exc:
        raise WorkerProvisioningError("WORKER_IRONIC_INTERNAL_NTP_IP_CONFIGURATION_REQUIRED") from exc
    timeout_seconds = payload.get("timeout_seconds", 1800)
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 30 <= timeout_seconds <= 7200
    ):
        raise WorkerProvisioningError("WORKER_PROVISION_TIMEOUT_INVALID")
    resources, selector, desired, namespace, ntp_ip = _prepare_resources(
        payload,
        configured_ntp,
    )
    active_runner = runner or _run_kubectl
    for resource in resources[:2]:
        _check_lease(lease_lost_event)
        _create_immutable_template(resource, active_runner)
    _check_lease(lease_lost_event)
    _apply_machine_deployment(resources[2], active_runner)
    deadline = monotonic() + timeout_seconds
    selector_text = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    last_state: dict[str, Any] = {}
    while monotonic() < deadline:
        _check_lease(lease_lost_event)
        md = _get_resource(
            active_runner,
            "machinedeployments.cluster.x-k8s.io",
            resources[2]["metadata"]["name"],
            namespace,
        )
        if md is None:
            raise WorkerProvisioningError("WORKER_MACHINE_DEPLOYMENT_DISAPPEARED")
        spec = md.get("spec", {})
        status = md.get("status", {})
        generation = md.get("metadata", {}).get("generation", 0)
        observed = status.get("observedGeneration", 0)
        replicas = spec.get("replicas", 0)
        machines_result = active_runner([
            "get", "machines.cluster.x-k8s.io",
            "--namespace", namespace,
            "--selector", selector_text,
            "-o", "json",
        ])
        if not machines_result["ok"]:
            raise WorkerProvisioningError("WORKER_MACHINE_INVENTORY_FAILED")
        try:
            machines = json.loads(machines_result["stdout"]).get("items", [])
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorkerProvisioningError("WORKER_MACHINE_INVENTORY_INVALID") from exc
        node_names = sorted({
            machine.get("status", {}).get("nodeRef", {}).get("name")
            for machine in machines
            if isinstance(machine.get("status", {}).get("nodeRef", {}).get("name"), str)
        })
        ready_nodes = []
        if node_names:
            nodes_result = active_runner(["get", "nodes", "-o", "json"])
            if not nodes_result["ok"]:
                raise WorkerProvisioningError("WORKER_NODE_INVENTORY_FAILED")
            try:
                node_document = json.loads(nodes_result["stdout"])
            except (TypeError, ValueError) as exc:
                raise WorkerProvisioningError("WORKER_NODE_INVENTORY_INVALID") from exc
            nodes = {
                item.get("metadata", {}).get("name"): item
                for item in node_document.get("items", [])
            }
            ready_nodes = [name for name in node_names if name in nodes and _is_ready(nodes[name])]
        last_state = {
            "machine_deployment": resources[2]["metadata"]["name"],
            "desired_replicas": desired,
            "observed_replicas": status.get("replicas", 0),
            "updated_replicas": status.get("updatedReplicas", 0),
            "ready_replicas": status.get("readyReplicas", 0),
            "available_replicas": status.get("availableReplicas", 0),
            "observed_generation": observed,
            "generation": generation,
            "machine_count": len(machines),
            "node_count": len(node_names),
            "ready_node_names": ready_nodes,
        }
        if (
            replicas == desired
            and observed >= generation
            and status.get("updatedReplicas", 0) == desired
            and status.get("readyReplicas", 0) == desired
            and status.get("availableReplicas", 0) == desired
            and len(machines) == desired
            and len(node_names) == desired
            and len(ready_nodes) == desired
        ):
            return {
                "status": "succeeded",
                "error_code": None,
                "output": {
                    **last_state,
                    "worker_ntp_source": ntp_ip,
                    "worker_ntp_sources": {
                        name: [ntp_ip] for name in ready_nodes
                    },
                    "worker_ntp_synchronized": sorted(ready_nodes),
                    "worker_ntp_bootstrap_injected": True,
                    "workers_ready": True,
                },
                "side_effects": True,
            }
        sleep(min(10.0, max(0.1, deadline - monotonic())))
    raise WorkerProvisioningError(
        "WORKER_PROVISIONING_TIMEOUT:" + json.dumps(last_state, sort_keys=True)
    )
