from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any

import yaml

from .cleanup_scope import CleanupScopeError, attest_cleanup_scope


class CapiPivotError(RuntimeError):
    pass


NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SOURCE_UID_ANNOTATION = "ai-agent-stack.io/source-cluster-uid"
STABLE_ID_ANNOTATION = "ai-agent-stack.io/stable-cluster-identity"
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TARGET_KUBECONFIG_PATH = Path("/tmp/target-agent-in-cluster.kubeconfig")


def _valid_name(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not NAME_PATTERN.fullmatch(value)
    ):
        raise CapiPivotError(f"CAPI_PIVOT_{field}_INVALID")
    return value


def _run(
    command: list[str],
    kubeconfig: str,
    timeout_seconds: int = 30,
    lease_lost_event: Event | None = None,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    if not Path(kubeconfig).is_file():
        raise CapiPivotError("CAPI_PIVOT_KUBECONFIG_MISSING")
    allowed_environment = (
        "PATH",
        "HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "XDG_CACHE_HOME",
    )
    environment = {
        key: os.environ[key]
        for key in allowed_environment
        if key in os.environ
    }
    environment["KUBECONFIG"] = kubeconfig
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise CapiPivotError("CAPI_PIVOT_COMMAND_START_FAILED") from exc
    started = time.monotonic()
    termination_reason = None
    stdout = ""
    stderr = ""
    for _ in iter(int, 1):
        try:
            stdout, stderr = process.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if lease_lost_event is not None and lease_lost_event.is_set():
                termination_reason = "COMMAND_LEASE_LOST"
            elif time.monotonic() - started > timeout_seconds:
                termination_reason = "COMMAND_TIMEOUT"
            if termination_reason:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                break
    if termination_reason:
        return {
            "ok": False,
            "returncode": process.returncode,
            "stdout": "",
            "stderr": stderr[-4000:],
            "error": termination_reason,
        }
    output_size = len(stdout.encode("utf-8"))
    if output_size > max_output_bytes:
        return {
            "ok": False,
            "returncode": process.returncode,
            "stdout": "",
            "stderr": stderr[-4000:],
            "error": "COMMAND_OUTPUT_LIMIT_EXCEEDED",
            "output_bytes": output_size,
        }
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr[-4000:],
    }


def _kubectl(kubeconfig: str, arguments: list[str]) -> dict[str, Any]:
    result = _run(
        ["kubectl", "--kubeconfig", kubeconfig, *arguments],
        kubeconfig,
        max_output_bytes=32 * 1024 * 1024,
    )
    if not result["ok"]:
        raise CapiPivotError("CAPI_PIVOT_KUBECTL_READ_FAILED")
    try:
        document = json.loads(result["stdout"])
    except ValueError as exc:
        raise CapiPivotError("CAPI_PIVOT_KUBECTL_JSON_INVALID") from exc
    if not isinstance(document, dict):
        raise CapiPivotError("CAPI_PIVOT_KUBECTL_OBJECT_REQUIRED")
    return document


def _cluster(kubeconfig: str, namespace: str, name: str) -> dict[str, Any] | None:
    result = _run(
        [
            "kubectl", "--kubeconfig", kubeconfig, "get",
            "cluster.cluster.x-k8s.io", name, "--namespace", namespace, "-o", "json",
        ],
        kubeconfig,
        max_output_bytes=2 * 1024 * 1024,
    )
    if result["ok"]:
        try:
            document = json.loads(result["stdout"])
        except ValueError as exc:
            raise CapiPivotError("CAPI_PIVOT_CLUSTER_JSON_INVALID") from exc
        if not isinstance(document, dict):
            raise CapiPivotError("CAPI_PIVOT_CLUSTER_OBJECT_INVALID")
        return document
    if "NotFound" in result["stderr"] or "not found" in result["stderr"].lower():
        return None
    raise CapiPivotError("CAPI_PIVOT_CLUSTER_READ_FAILED")


def _version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_VERSION_INVALID")
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_VERSION_INVALID")
    return tuple(int(part) for part in match.groups())


def _image_version(image: str, deployment: dict[str, Any]) -> tuple[int, int, int]:
    if not isinstance(image, str) or not image:
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_IMAGE_INVALID")
    reference = image.split("@", 1)[0]
    last_component = reference.rsplit("/", 1)[-1]
    candidate = last_component.rsplit(":", 1)[-1] if ":" in last_component else ""
    if not VERSION_PATTERN.fullmatch(candidate):
        metadata = deployment.get("metadata", {})
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        candidate = (
            labels.get("app.kubernetes.io/version")
            or annotations.get("app.kubernetes.io/version")
            or annotations.get("ai-agent-stack.io/provider-version")
            or ""
        )
    return _version(candidate)


def _kubernetes_cluster_uid(kubeconfig: str) -> str:
    document = _kubectl(kubeconfig, ["get", "namespace", "kube-system", "-o", "json"])
    uid = document.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise CapiPivotError("CAPI_PIVOT_KUBERNETES_CLUSTER_UID_MISSING")
    return uid


def _ready_masters(kubeconfig: str) -> set[str]:
    document = _kubectl(kubeconfig, ["get", "nodes", "-o", "json"])
    masters = set()
    for node in document.get("items", []):
        labels = node.get("metadata", {}).get("labels", {})
        is_master = (
            "node-role.kubernetes.io/control-plane" in labels
            or "node-role.kubernetes.io/master" in labels
        )
        ready = any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in node.get("status", {}).get("conditions", [])
        )
        if is_master and ready:
            masters.add(node.get("metadata", {}).get("name"))
    masters.discard(None)
    if len(masters) < 3:
        raise CapiPivotError("CAPI_PIVOT_THREE_READY_TARGET_MASTERS_REQUIRED")
    return masters


def _deployment(
    kubeconfig: str,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _kubectl(
        kubeconfig,
        ["get", "deployment", name, "--namespace", namespace, "-o", "json"],
    )


def _target_controllers_ready(
    kubeconfig: str,
    master_names: set[str],
    controllers: Any,
) -> dict[str, Any]:
    if not isinstance(controllers, list) or not controllers:
        raise CapiPivotError("CAPI_PIVOT_TARGET_CONTROLLER_LIST_REQUIRED")
    evidence = []
    for item in controllers:
        if not isinstance(item, dict):
            raise CapiPivotError("CAPI_PIVOT_TARGET_CONTROLLER_ENTRY_INVALID")
        namespace = _valid_name(item.get("namespace"), "CONTROLLER_NAMESPACE")
        name = _valid_name(item.get("name"), "CONTROLLER_NAME")
        minimum = item.get("minimum_ready_replicas", 1)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise CapiPivotError("CAPI_PIVOT_CONTROLLER_REPLICA_REQUIREMENT_INVALID")
        deployment = _deployment(kubeconfig, namespace, name)
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        generation = deployment.get("metadata", {}).get("generation", 0)
        observed = status.get("observedGeneration", 0)
        if (
            status.get("readyReplicas", 0) < minimum
            or status.get("availableReplicas", 0) < minimum
            or observed < generation
        ):
            raise CapiPivotError(f"CAPI_PIVOT_TARGET_CONTROLLER_NOT_READY:{name}")
        tolerations = spec.get("template", {}).get("spec", {}).get("tolerations", [])
        tolerates_master = any(
            item.get("key") in (
                "node-role.kubernetes.io/control-plane",
                "node-role.kubernetes.io/master",
            )
            and (
                item.get("operator") == "Exists"
                or item.get("effect") in ("NoSchedule", None)
            )
            for item in tolerations
        )
        if not tolerates_master:
            raise CapiPivotError(f"CAPI_PIVOT_CONTROLLER_MISSING_MASTER_TOLERATION:{name}")
        selector = spec.get("selector", {}).get("matchLabels", {})
        if not isinstance(selector, dict) or not selector:
            raise CapiPivotError(f"CAPI_PIVOT_CONTROLLER_SELECTOR_INVALID:{name}")
        label_selector = ",".join(
            f"{key}={value}" for key, value in sorted(selector.items())
        )
        pods = _kubectl(
            kubeconfig,
            ["get", "pods", "--namespace", namespace, "-l", label_selector, "-o", "json"],
        )
        ready_pods = []
        for pod in pods.get("items", []):
            pod_ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
            node_name = pod.get("spec", {}).get("nodeName")
            if pod_ready and node_name in master_names:
                ready_pods.append(pod.get("metadata", {}).get("name"))
        if len(ready_pods) < minimum:
            raise CapiPivotError(f"CAPI_PIVOT_CONTROLLER_NOT_RUNNING_ON_MASTERS:{name}")
        evidence.append({
            "namespace": namespace,
            "name": name,
            "ready_replicas": len(ready_pods),
            "scheduled_on_target_masters": True,
        })
    return {"ready": True, "controllers": evidence}


def _provider_versions(
    source_kubeconfig: str,
    target_kubeconfig: str,
    providers: Any,
) -> list[dict[str, Any]]:
    if not isinstance(providers, list) or not providers:
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_VERSION_MATRIX_REQUIRED")
    evidence = []
    for item in providers:
        if not isinstance(item, dict):
            raise CapiPivotError("CAPI_PIVOT_PROVIDER_ENTRY_INVALID")
        provider = _valid_name(item.get("provider"), "PROVIDER_NAME")
        source_ref = item.get("source")
        target_ref = item.get("target")
        if not isinstance(source_ref, dict) or not isinstance(target_ref, dict):
            raise CapiPivotError("CAPI_PIVOT_PROVIDER_DEPLOYMENT_REFERENCE_INVALID")
        source = _deployment(
            source_kubeconfig,
            _valid_name(source_ref.get("namespace"), "SOURCE_PROVIDER_NAMESPACE"),
            _valid_name(source_ref.get("name"), "SOURCE_PROVIDER_DEPLOYMENT"),
        )
        target = _deployment(
            target_kubeconfig,
            _valid_name(target_ref.get("namespace"), "TARGET_PROVIDER_NAMESPACE"),
            _valid_name(target_ref.get("name"), "TARGET_PROVIDER_DEPLOYMENT"),
        )
        if target.get("status", {}).get("readyReplicas", 0) < 1:
            raise CapiPivotError(f"CAPI_PIVOT_TARGET_PROVIDER_NOT_READY:{provider}")
        source_status = source.get("status", {})
        if source_status.get("readyReplicas", 0) < 1:
            raise CapiPivotError(f"CAPI_PIVOT_SOURCE_PROVIDER_NOT_READY:{provider}")
        source_container_list = (
            source.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        target_container_list = (
            target.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        source_containers = {
            item.get("name"): item.get("image") for item in source_container_list
        }
        target_containers = {
            item.get("name"): item.get("image") for item in target_container_list
        }
        if (
            not source_containers
            or None in source_containers
            or set(source_containers) != set(target_containers)
        ):
            raise CapiPivotError(f"CAPI_PIVOT_PROVIDER_CONTAINER_SET_MISMATCH:{provider}")
        for container_name, source_image in source_containers.items():
            source_version = _image_version(source_image, source)
            target_version = _image_version(target_containers[container_name], target)
            if target_version < source_version:
                raise CapiPivotError(f"CAPI_PIVOT_TARGET_PROVIDER_OLDER_THAN_SOURCE:{provider}")
        evidence.append({
            "provider": provider,
            "source_images": list(source_containers.values()),
            "target_images": list(target_containers.values()),
            "target_version_at_least_source": True,
        })
    return evidence


def _hardware_identity(host: dict[str, Any]) -> str:
    spec = host.get("spec", {})
    hardware = host.get("status", {}).get("hardware", {})
    system_uuid = hardware.get("systemUUID")
    serial = hardware.get("systemSerialNumber")
    boot_mac = spec.get("bootMACAddress")
    bmc_address = spec.get("bmc", {}).get("address")
    if isinstance(system_uuid, str) and system_uuid.strip():
        digest = hashlib.sha256(system_uuid.strip().lower().encode()).hexdigest()
        return "system-uuid:sha256:" + digest
    if isinstance(serial, str) and serial.strip():
        digest = hashlib.sha256(serial.strip().lower().encode()).hexdigest()
        return "serial:sha256:" + digest
    if (
        isinstance(boot_mac, str) and boot_mac.strip()
        and isinstance(bmc_address, str) and bmc_address.strip()
    ):
        safe_bmc_address = re.sub(
            r"(?<=://)[^/@]+@",
            "",
            bmc_address.strip(),
            count=1,
        )
        material = safe_bmc_address.lower() + "|" + boot_mac.strip().lower()
        digest = hashlib.sha256(material.encode()).hexdigest()
        return "bmc-bootmac:sha256:" + digest
    raise CapiPivotError("CAPI_PIVOT_BMH_STABLE_HARDWARE_IDENTITY_MISSING")


def _bmh_inventory(kubeconfig: str, namespace: str) -> list[dict[str, str]]:
    document = _kubectl(
        kubeconfig,
        ["get", "baremetalhosts.metal3.io", "--namespace", namespace, "-o", "json"],
    )
    identities = []
    hosts = document.get("items", [])
    if len(hosts) > 2500:
        raise CapiPivotError("CAPI_PIVOT_BMH_INVENTORY_LIMIT_EXCEEDED")
    for host in hosts:
        metadata = host.get("metadata", {})
        namespace = metadata.get("namespace")
        name = metadata.get("name")
        if not isinstance(namespace, str) or not isinstance(name, str):
            raise CapiPivotError("CAPI_PIVOT_BMH_METADATA_INVALID")
        identities.append({
            "namespace": namespace,
            "name": name,
            "hardware_identity": _hardware_identity(host),
        })
    hardware_ids = [item["hardware_identity"] for item in identities]
    if not identities:
        raise CapiPivotError("CAPI_PIVOT_BMH_INVENTORY_EMPTY")
    if len(hardware_ids) != len(set(hardware_ids)):
        raise CapiPivotError("CAPI_PIVOT_BMH_HARDWARE_IDENTITY_DUPLICATE")
    identities.sort(key=lambda item: (item["namespace"], item["name"], item["hardware_identity"]))
    return identities


def _wait_for_target_bmh_inventory(
    kubeconfig: str,
    namespace: str,
    expected: list[dict[str, str]],
    lease_lost_event: Event | None,
    attempts: int = 60,
) -> list[dict[str, str]]:
    expected_set = {
        (item["namespace"], item["name"], item["hardware_identity"])
        for item in expected
    }
    last_error = "CAPI_PIVOT_TARGET_BMH_INVENTORY_NOT_RECONCILED"
    for _ in range(attempts):
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise CapiPivotError("COMMAND_LEASE_LOST_DURING_BMH_POSTFLIGHT")
        try:
            observed = _bmh_inventory(kubeconfig, namespace)
            observed_set = {
                (item["namespace"], item["name"], item["hardware_identity"])
                for item in observed
            }
            if observed_set == expected_set:
                return observed
        except CapiPivotError as exc:
            last_error = str(exc)
        time.sleep(5)
    raise CapiPivotError(last_error)


def _inventory_digest(identities: list[dict[str, str]]) -> str:
    encoded = json.dumps(
        identities,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_target_kubeconfig() -> str:
    configured = os.environ.get("CAPI_PIVOT_TARGET_KUBECONFIG") or os.environ.get("KUBECONFIG")
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_file():
            raise CapiPivotError("CAPI_PIVOT_TARGET_KUBECONFIG_MISSING")
        return str(configured_path)
    path = TARGET_KUBECONFIG_PATH
    if path.is_file():
        return str(path)
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token_path = SERVICE_ACCOUNT_DIR / "token"
    ca_path = SERVICE_ACCOUNT_DIR / "ca.crt"
    try:
        port_number = int(port)
    except (TypeError, ValueError) as exc:
        raise CapiPivotError(
            "CAPI_PIVOT_TARGET_IN_CLUSTER_CREDENTIALS_UNAVAILABLE"
        ) from exc
    if (
        not isinstance(host, str)
        or not host
        or any(character.isspace() or character in "/?#" for character in host)
        or not 1 <= port_number <= 65535
        or not token_path.is_file()
        or not ca_path.is_file()
    ):
        raise CapiPivotError("CAPI_PIVOT_TARGET_IN_CLUSTER_CREDENTIALS_UNAVAILABLE")
    server_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": "target-in-cluster",
            "cluster": {
                "server": f"https://{server_host}:{port_number}",
                "certificate-authority": str(ca_path),
            },
        }],
        "users": [{
            "name": "target-agent",
            "user": {"tokenFile": str(token_path)},
        }],
        "contexts": [{
            "name": "target-agent@target-in-cluster",
            "context": {
                "cluster": "target-in-cluster",
                "user": "target-agent",
            },
        }],
        "current-context": "target-agent@target-in-cluster",
    }
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            yaml.safe_dump(document, sort_keys=False),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise CapiPivotError("CAPI_PIVOT_TARGET_KUBECONFIG_WRITE_FAILED") from exc
    return str(path)


def _validate_pivot_inputs(command: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    payload = command.get("payload")
    if not isinstance(payload, dict):
        raise CapiPivotError("CAPI_PIVOT_PAYLOAD_REQUIRED")
    payload = dict(payload)
    if not payload.get("deployment_id"):
        payload["deployment_id"] = command.get("deployment_id")
    _valid_name(payload.get("namespace"), "NAMESPACE")
    _valid_name(payload.get("cluster_name"), "CLUSTER_NAME")
    _valid_name(payload.get("deployment_id"), "DEPLOYMENT_ID")
    stable_identity = payload.get("stable_cluster_identity")
    expected_source_uid = payload.get("expected_source_cluster_uid")
    expected_bmh = payload.get("expected_source_bmh_identities")
    if (
        not isinstance(stable_identity, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", stable_identity)
    ):
        raise CapiPivotError("CAPI_PIVOT_STABLE_CLUSTER_IDENTITY_REQUIRED")
    if not isinstance(expected_source_uid, str) or not expected_source_uid:
        raise CapiPivotError("CAPI_PIVOT_EXPECTED_SOURCE_CLUSTER_UID_REQUIRED")
    if not isinstance(expected_bmh, list) or not expected_bmh:
        raise CapiPivotError("CAPI_PIVOT_EXPECTED_BMH_INVENTORY_REQUIRED")
    source = os.environ.get("CAPI_PIVOT_SOURCE_KUBECONFIG", "")
    if not Path(source).is_file():
        raise CapiPivotError("CAPI_PIVOT_SOURCE_KUBECONFIG_MISSING")
    target = _ensure_target_kubeconfig()
    return payload, source, target


def _provider_preflight(
    payload: dict[str, Any],
    source: str,
    target: str,
) -> dict[str, Any]:
    masters = _ready_masters(target)
    controller_specs = payload.get("target_controller_deployments")
    provider_specs = payload.get("provider_deployments")
    if not isinstance(controller_specs, list) or not isinstance(provider_specs, list):
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_CONTROLLER_MATRIX_INVALID")
    controller_refs = {
        (item.get("namespace"), item.get("name"))
        for item in controller_specs
        if isinstance(item, dict)
    }
    provider_target_refs = {
        (item.get("target", {}).get("namespace"), item.get("target", {}).get("name"))
        for item in provider_specs
        if isinstance(item, dict) and isinstance(item.get("target"), dict)
    }
    if not provider_target_refs or not provider_target_refs.issubset(controller_refs):
        raise CapiPivotError("CAPI_PIVOT_PROVIDER_MISSING_MASTER_PLACEMENT_CHECK")
    controllers = _target_controllers_ready(
        target,
        masters,
        controller_specs,
    )
    versions = _provider_versions(
        source,
        target,
        payload.get("provider_deployments"),
    )
    return {
        "target_ready_master_names": sorted(masters),
        "target_controllers": controllers,
        "provider_versions": versions,
    }


def execute_capi_pivot(
    command: dict[str, Any],
    lease_lost_event: Event | None = None,
) -> dict[str, Any]:
    action = command.get("action")
    side_effects = False
    try:
        payload, source, target = _validate_pivot_inputs(command)
        namespace = _valid_name(payload.get("namespace"), "NAMESPACE")
        cluster_name = _valid_name(payload.get("cluster_name"), "CLUSTER_NAME")
        deployment_id = payload["deployment_id"]
        stable_identity = payload["stable_cluster_identity"].strip()
        source_api_uid = _kubernetes_cluster_uid(source)
        target_api_uid = _kubernetes_cluster_uid(target)
        if source_api_uid == target_api_uid:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_AND_TARGET_API_ARE_IDENTICAL")
        preflight = _provider_preflight(payload, source, target)
        source_cluster = _cluster(source, namespace, cluster_name)
        target_cluster = _cluster(target, namespace, cluster_name)
        if source_cluster is None and target_cluster is not None:
            annotations = target_cluster.get("metadata", {}).get("annotations", {})
            if (
                annotations.get(SOURCE_UID_ANNOTATION) == payload["expected_source_cluster_uid"]
                and annotations.get(STABLE_ID_ANNOTATION) == stable_identity
            ):
                target_bmhs = _bmh_inventory(target, namespace)
                expected_set = {
                    (item["namespace"], item["name"], item["hardware_identity"])
                    for item in payload["expected_source_bmh_identities"]
                }
                target_set = {
                    (item["namespace"], item["name"], item["hardware_identity"])
                    for item in target_bmhs
                }
                if expected_set != target_set:
                    raise CapiPivotError("CAPI_PIVOT_REPLAY_BMH_IDENTITY_SET_MISMATCH")
                try:
                    cleanup_scope = attest_cleanup_scope(
                        source,
                        target,
                        deployment_id,
                        annotations[SOURCE_UID_ANNOTATION],
                        _run,
                        lease_lost_event,
                    )
                except CleanupScopeError as exc:
                    side_effects = side_effects or exc.side_effects
                    raise CapiPivotError(str(exc)) from exc
                side_effects = cleanup_scope["mutated_resource_count"] > 0
                return {
                    "status": "succeeded",
                    "error_code": None,
                    "output": {
                        "already_pivoted": True,
                        "capi_pivot_succeeded": True,
                        "source_cluster_uid": annotations[SOURCE_UID_ANNOTATION],
                        "target_cluster_uid": target_cluster.get("metadata", {}).get("uid"),
                        "target_source_cluster_uid_annotation": annotations[SOURCE_UID_ANNOTATION],
                        "source_stable_cluster_identity": stable_identity,
                        "target_stable_cluster_identity": annotations[STABLE_ID_ANNOTATION],
                        "source_cluster_absent": True,
                        "target_cluster_ownership_verified": True,
                        "source_bmh_identities": payload["expected_source_bmh_identities"],
                        "target_bmh_identities": target_bmhs,
                        "source_bmh_digest": _inventory_digest(
                            payload["expected_source_bmh_identities"]
                        ),
                        "target_bmh_digest": _inventory_digest(target_bmhs),
                        "target_controllers_ready": preflight["target_controllers"]["ready"],
                        "provider_preflight": preflight,
                        "cleanup_scope_attested": cleanup_scope["cleanup_scope_attested"],
                        "source_cleanup_resource_count": cleanup_scope[
                            "source_cleanup_resource_count"
                        ],
                        "target_observer_secret_source_uid_attested": cleanup_scope[
                            "target_observer_secret_source_uid_attested"
                        ],
                        "optional_target_kubeconfig_marked": cleanup_scope[
                            "optional_target_kubeconfig_marked"
                        ],
                        "cleanup_scope_mutated_resource_count": cleanup_scope[
                            "mutated_resource_count"
                        ],
                    },
                    "side_effects": side_effects,
                }
        if source_cluster is None or target_cluster is not None:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_TARGET_CLUSTER_STATE_CONFLICT")
        source_uid = source_cluster.get("metadata", {}).get("uid")
        annotations = source_cluster.get("metadata", {}).get("annotations", {})
        if not isinstance(source_uid, str) or not source_uid:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_CLUSTER_UID_MISSING")
        if source_uid != payload["expected_source_cluster_uid"]:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_CLUSTER_UID_CHANGED_SINCE_PLAN")
        existing_source_uid = annotations.get(SOURCE_UID_ANNOTATION)
        existing_stable_identity = annotations.get(STABLE_ID_ANNOTATION)
        if existing_source_uid not in (None, source_uid):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_UID_ANNOTATION_CONFLICT")
        if existing_stable_identity not in (None, stable_identity):
            raise CapiPivotError("CAPI_PIVOT_STABLE_IDENTITY_ANNOTATION_CONFLICT")
        source_bmhs = _bmh_inventory(source, namespace)
        expected_bmh_set = {
            (item.get("namespace"), item.get("name"), item.get("hardware_identity"))
            for item in payload["expected_source_bmh_identities"]
            if isinstance(item, dict)
        }
        source_bmh_set = {
            (item["namespace"], item["name"], item["hardware_identity"])
            for item in source_bmhs
        }
        if source_bmh_set != expected_bmh_set:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_INVENTORY_CHANGED_SINCE_PLAN")
        source_cluster_list = _kubectl(
            source,
            ["get", "clusters.cluster.x-k8s.io", "--namespace", namespace, "-o", "json"],
        )
        if len(source_cluster_list.get("items", [])) != 1:
            raise CapiPivotError("CAPI_PIVOT_NAMESPACE_MUST_CONTAIN_EXACTLY_ONE_CLUSTER")
        move_command = [
            "clusterctl",
            "move",
            f"--to-kubeconfig={target}",
            f"--namespace={namespace}",
        ]
        if action == "target.capi.pivot.dry_run":
            dry_run = _run(
                [*move_command, "--dry-run"],
                source,
                timeout_seconds=300,
                lease_lost_event=lease_lost_event,
            )
            if not dry_run["ok"]:
                raise CapiPivotError("CAPI_PIVOT_DRY_RUN_FAILED")
            return {
                "status": "succeeded",
                "error_code": None,
                "output": {
                    "dry_run_passed": True,
                    "source_cluster_uid": source_uid,
                    "source_bmh_identities": source_bmhs,
                    "source_bmh_digest": _inventory_digest(source_bmhs),
                    "provider_preflight": preflight,
                    "clusterctl_output": dry_run["stdout"][-8000:],
                    "side_effects": False,
                },
                "side_effects": False,
            }
        if action != "target.capi.pivot":
            raise CapiPivotError("CAPI_PIVOT_ACTION_UNSUPPORTED")
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise CapiPivotError("COMMAND_LEASE_LOST_BEFORE_CAPI_PIVOT")
        annotate = _run(
            [
                "kubectl", "--kubeconfig", source, "annotate",
                "cluster.cluster.x-k8s.io", cluster_name,
                f"{SOURCE_UID_ANNOTATION}={source_uid}",
                f"{STABLE_ID_ANNOTATION}={stable_identity}",
                "--namespace", namespace, "--overwrite",
            ],
            source,
            timeout_seconds=30,
            lease_lost_event=lease_lost_event,
        )
        if not annotate["ok"]:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_IDENTITY_ANNOTATION_FAILED")
        side_effects = True
        move_timeout = payload.get("move_timeout_seconds", 1800)
        if not isinstance(move_timeout, int) or not 60 <= move_timeout <= 3600:
            raise CapiPivotError("CAPI_PIVOT_MOVE_TIMEOUT_INVALID")
        moved = _run(
            move_command,
            source,
            timeout_seconds=move_timeout,
            lease_lost_event=lease_lost_event,
        )
        if not moved["ok"]:
            raise CapiPivotError(
                "CAPI_PIVOT_MOVE_FAILED:" + str(moved.get("error") or moved["returncode"])
            )
        target_after = _cluster(target, namespace, cluster_name)
        source_after = _cluster(source, namespace, cluster_name)
        if target_after is None or source_after is not None:
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_SOURCE_TARGET_STATE_INVALID")
        target_annotations = target_after.get("metadata", {}).get("annotations", {})
        if target_annotations.get(SOURCE_UID_ANNOTATION) != source_uid:
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_SOURCE_UID_ANNOTATION_MISMATCH")
        if target_annotations.get(STABLE_ID_ANNOTATION) != stable_identity:
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_STABLE_IDENTITY_MISMATCH")
        target_after_preflight = _target_controllers_ready(
            target,
            preflight["target_ready_master_names"],
            payload.get("target_controller_deployments"),
        )
        target_bmhs = _wait_for_target_bmh_inventory(
            target,
            namespace,
            source_bmhs,
            lease_lost_event,
        )
        source_set = {
            (item["namespace"], item["name"], item["hardware_identity"])
            for item in source_bmhs
        }
        target_set = {
            (item["namespace"], item["name"], item["hardware_identity"])
            for item in target_bmhs
        }
        if source_set != target_set:
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_BMH_IDENTITY_SET_MISMATCH")
        target_uid = target_after.get("metadata", {}).get("uid")
        if not isinstance(target_uid, str) or not target_uid:
            raise CapiPivotError("CAPI_PIVOT_TARGET_CLUSTER_UID_MISSING")
        try:
            cleanup_scope = attest_cleanup_scope(
                source,
                target,
                deployment_id,
                source_uid,
                _run,
                lease_lost_event,
            )
        except CleanupScopeError as exc:
            side_effects = side_effects or exc.side_effects
            raise CapiPivotError(str(exc)) from exc
        return {
            "status": "succeeded",
            "error_code": None,
            "output": {
                "capi_pivot_succeeded": True,
                "source_cluster_uid": source_uid,
                "target_cluster_uid": target_uid,
                "target_source_cluster_uid_annotation": target_annotations[SOURCE_UID_ANNOTATION],
                "source_stable_cluster_identity": stable_identity,
                "target_stable_cluster_identity": target_annotations[STABLE_ID_ANNOTATION],
                "source_cluster_absent": True,
                "target_cluster_ownership_verified": True,
                "target_controllers_ready": target_after_preflight["ready"],
                "source_bmh_identities": source_bmhs,
                "target_bmh_identities": target_bmhs,
                "source_bmh_digest": _inventory_digest(source_bmhs),
                "target_bmh_digest": _inventory_digest(target_bmhs),
                "provider_preflight": preflight,
                "clusterctl_output": moved["stdout"][-8000:],
                "cleanup_scope_attested": cleanup_scope["cleanup_scope_attested"],
                "source_cleanup_resource_count": cleanup_scope[
                    "source_cleanup_resource_count"
                ],
                "target_observer_secret_source_uid_attested": cleanup_scope[
                    "target_observer_secret_source_uid_attested"
                ],
                "optional_target_kubeconfig_marked": cleanup_scope[
                    "optional_target_kubeconfig_marked"
                ],
                "cleanup_scope_mutated_resource_count": cleanup_scope[
                    "mutated_resource_count"
                ],
            },
            "side_effects": True,
        }
    except (CapiPivotError, ValueError, TypeError, KeyError) as exc:
        return {
            "status": "blocked",
            "error_code": str(exc) or "CAPI_PIVOT_BLOCKED",
            "output": {},
            "side_effects": side_effects,
        }
