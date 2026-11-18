from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


class Metal3SkillError(ValueError):
    def __init__(self, code: str, side_effects: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.side_effects = side_effects


BMH_API_VERSION = "metal3.io/v1alpha1"
CAPI_KINDS = {
    "Cluster",
    "Metal3Cluster",
    "Metal3MachineTemplate",
    "Metal3DataTemplate",
    "KubeadmControlPlane",
    "KubeadmConfigTemplate",
    "MachineDeployment",
}
CAPI_API_VERSIONS = {
    "Cluster": {"cluster.x-k8s.io/v1beta2"},
    "MachineDeployment": {"cluster.x-k8s.io/v1beta2"},
    "Metal3Cluster": {"infrastructure.cluster.x-k8s.io/v1beta2"},
    "Metal3MachineTemplate": {"infrastructure.cluster.x-k8s.io/v1beta2"},
    "Metal3DataTemplate": {"infrastructure.cluster.x-k8s.io/v1beta2"},
    "KubeadmControlPlane": {"controlplane.cluster.x-k8s.io/v1beta2"},
    "KubeadmConfigTemplate": {"bootstrap.cluster.x-k8s.io/v1beta2"},
}
BMH_KIND = "BareMetalHost"
FIELD_MANAGER = "ai-agent-eph-agent"
MAX_MANIFESTS = 200
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _namespace(value: Any) -> str:
    configured_namespace = os.environ.get("EPH_CAPI_NAMESPACE", "metal3")
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)
        or len(value) > 63
    ):
        raise Metal3SkillError("METAL3_NAMESPACE_INVALID")
    if value != configured_namespace:
        raise Metal3SkillError("METAL3_NAMESPACE_NOT_ALLOWED")
    return value


def _validate_bundle(
    manifests: Any,
    namespace: str,
    allowed_kinds: set[str],
    expected_api_versions: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(manifests, list) or not 1 <= len(manifests) <= MAX_MANIFESTS:
        raise Metal3SkillError("METAL3_MANIFEST_BUNDLE_INVALID")
    validated = []
    seen = set()
    for manifest in manifests:
        if not isinstance(manifest, dict):
            raise Metal3SkillError("METAL3_MANIFEST_INVALID")
        kind = manifest.get("kind")
        api_version = manifest.get("apiVersion")
        metadata = manifest.get("metadata")
        if kind not in allowed_kinds or not isinstance(api_version, str):
            raise Metal3SkillError("METAL3_RESOURCE_KIND_NOT_ALLOWED")
        if expected_api_versions and api_version not in expected_api_versions.get(kind, set()):
            raise Metal3SkillError("METAL3_RESOURCE_API_VERSION_NOT_ALLOWED")
        if not isinstance(metadata, dict):
            raise Metal3SkillError("METAL3_RESOURCE_METADATA_REQUIRED")
        name = metadata.get("name")
        resource_namespace = metadata.get("namespace", namespace)
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name)
            or len(name) > 253
            or resource_namespace != namespace
        ):
            raise Metal3SkillError("METAL3_RESOURCE_IDENTITY_INVALID")
        key = (api_version, kind, resource_namespace, name)
        if key in seen:
            raise Metal3SkillError("METAL3_RESOURCE_DUPLICATE")
        seen.add(key)
        manifest = dict(manifest)
        manifest["metadata"] = {**metadata, "namespace": namespace}
        if "status" in manifest:
            raise Metal3SkillError("METAL3_STATUS_MUST_NOT_BE_APPLIED")
        validated.append(manifest)
    return validated


def _validate_bmh(manifest: dict[str, Any]) -> None:
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        raise Metal3SkillError("METAL3_BMH_SPEC_REQUIRED")
    boot_mac = spec.get("bootMACAddress")
    bmc = spec.get("bmc")
    if not isinstance(boot_mac, str) or not MAC_PATTERN.fullmatch(boot_mac):
        raise Metal3SkillError("METAL3_BMH_BOOT_MAC_INVALID")
    if not isinstance(bmc, dict):
        raise Metal3SkillError("METAL3_BMH_BMC_CONFIG_REQUIRED")
    address = bmc.get("address")
    secret_name = bmc.get("credentialsName")
    if (
        not isinstance(address, str)
        or "://" not in address
        or "@" in address.split("://", 1)[-1].split("/", 1)[0]
        or not isinstance(secret_name, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", secret_name)
    ):
        raise Metal3SkillError("METAL3_BMH_BMC_CREDENTIAL_REFERENCE_INVALID")
    if not isinstance(spec.get("online", False), bool):
        raise Metal3SkillError("METAL3_BMH_ONLINE_FLAG_INVALID")


def _invoke(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int,
) -> dict[str, Any]:
    result = runner(arguments, timeout=timeout, max_output_bytes=MAX_MANIFEST_BYTES)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise Metal3SkillError("METAL3_KUBERNETES_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise Metal3SkillError("METAL3_KUBERNETES_OPERATION_FAILED")
    return result


def apply_baremetalhosts(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise Metal3SkillError("COMMAND_LEASE_LOST_BEFORE_METAL3_APPLY")
    namespace = _namespace(payload.get("namespace", "metal3"))
    manifests = _validate_bundle(
        payload.get("manifests"),
        namespace,
        {BMH_KIND},
        {BMH_KIND: {BMH_API_VERSION}},
    )
    for manifest in manifests:
        _validate_bmh(manifest)
    secret_names = sorted({
        manifest["spec"]["bmc"]["credentialsName"]
        for manifest in manifests
    })
    for secret_name in secret_names:
        try:
            _invoke(
                runner,
                ["get", "secret", secret_name, "--namespace", namespace, "-o", "name"],
                30,
            )
        except Metal3SkillError as exc:
            raise Metal3SkillError(
                "METAL3_BMC_CREDENTIAL_SECRET_MISSING"
            ) from exc
    return _apply_bundle(manifests, runner, lease_lost_event, "METAL3_BMH")


def apply_capi_cluster_resources(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    namespace = _namespace(payload.get("namespace", "metal3"))
    manifests = _validate_bundle(
        payload.get("manifests"),
        namespace,
        CAPI_KINDS,
        CAPI_API_VERSIONS,
    )
    return _apply_bundle(manifests, runner, lease_lost_event, "CAPI_CLUSTER")


def _apply_bundle(
    manifests: list[dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
    resource_prefix: str,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise Metal3SkillError("COMMAND_LEASE_LOST_BEFORE_METAL3_APPLY")
    serialized = yaml.safe_dump_all(manifests, sort_keys=False)
    encoded = serialized.encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise Metal3SkillError("METAL3_MANIFEST_BUNDLE_TOO_LARGE")
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(serialized)
            path = Path(handle.name)
        dry_run = _invoke(
            runner,
            [
                "apply",
                "--server-side",
                f"--field-manager={FIELD_MANAGER}",
                "--dry-run=server",
                "-f",
                str(path),
            ],
            120,
        )
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise Metal3SkillError("COMMAND_LEASE_LOST_AFTER_METAL3_DRY_RUN")
        try:
            applied = _invoke(
                runner,
                [
                    "apply",
                    "--server-side",
                    f"--field-manager={FIELD_MANAGER}",
                    "-f",
                    str(path),
                ],
                300,
            )
        except Metal3SkillError as exc:
            exc.side_effects = True
            raise
        evidence = {
            "resource_type": resource_prefix,
            "resource_count": len(manifests),
            "resource_names": [item["metadata"]["name"] for item in manifests],
            "server_side_dry_run_passed": True,
            "server_side_apply_succeeded": True,
            "dry_run_output": str(dry_run.get("stdout", ""))[-2000:],
            "apply_output": str(applied.get("stdout", ""))[-4000:],
            "side_effects": True,
        }
        if resource_prefix == "METAL3_BMH":
            evidence["bmc_secret_references_verified"] = True
            evidence["bmc_secret_reference_count"] = len({
                item["spec"]["bmc"]["credentialsName"] for item in manifests
            })
        return evidence
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def observe_baremetalhosts(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    namespace = _namespace(payload.get("namespace", "metal3"))
    result = _invoke(
        runner,
        ["get", "baremetalhosts.metal3.io", "--namespace", namespace, "-o", "json"],
        60,
    )
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise Metal3SkillError("METAL3_BMH_INVENTORY_INVALID") from exc
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list) or len(items) > 2500:
        raise Metal3SkillError("METAL3_BMH_INVENTORY_INVALID")
    hosts = []
    for item in items:
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        provisioning = status.get("provisioning", {})
        hardware = status.get("hardware", {})
        hosts.append({
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace", namespace),
            "boot_mac_address": spec.get("bootMACAddress"),
            "online": spec.get("online", False),
            "provisioning_state": provisioning.get("state"),
            "operational_status": status.get("operationalStatus"),
            "error_type": status.get("errorType"),
            "error_message": str(status.get("errorMessage", ""))[:1000],
            "hardware": {
                "system_uuid": hardware.get("systemUUID"),
                "serial_number": hardware.get("systemSerialNumber"),
                "cpu": hardware.get("cpu"),
                "ram_mib": hardware.get("ramMebibytes"),
                "nics": hardware.get("nics", []),
                "storage": hardware.get("storage", []),
            },
        })
    return {
        "namespace": namespace,
        "host_count": len(hosts),
        "available_count": sum(
            host["provisioning_state"] in {"available", "provisioned", "ready"}
            and host["operational_status"] == "OK"
            and not host["error_message"]
            for host in hosts
        ),
        "hosts": hosts,
        "side_effects": False,
    }


def wait_for_baremetalhosts_ready(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    namespace = _namespace(payload.get("namespace", "metal3"))
    host_names = payload.get("host_names")
    timeout_seconds = payload.get("timeout_seconds", 1800)
    if (
        not isinstance(host_names, list)
        or not 1 <= len(host_names) <= 2500
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name)
            for name in host_names
        )
        or len(host_names) != len(set(host_names))
    ):
        raise Metal3SkillError("METAL3_BMH_WAIT_HOST_LIST_INVALID")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 30 <= timeout_seconds <= 3600
    ):
        raise Metal3SkillError("METAL3_BMH_WAIT_TIMEOUT_INVALID")
    deadline = monotonic() + timeout_seconds
    pending = set(host_names)
    delay = 3.0
    observed = {}
    while pending and monotonic() < deadline:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise Metal3SkillError("COMMAND_LEASE_LOST_DURING_METAL3_WAIT")
        inventory = observe_baremetalhosts({"namespace": namespace}, runner)
        by_name = {host["name"]: host for host in inventory["hosts"]}
        missing = pending - set(by_name)
        if missing:
            raise Metal3SkillError("METAL3_BMH_NOT_FOUND:" + ",".join(sorted(missing)))
        for name in tuple(pending):
            host = by_name[name]
            if host.get("error_message") or host.get("error_type"):
                raise Metal3SkillError("METAL3_BMH_PROVISIONING_FAILED:" + name)
            if (
                host.get("provisioning_state") in {"available", "provisioned", "ready"}
                and host.get("operational_status") == "OK"
            ):
                pending.remove(name)
                observed[name] = host
        if pending:
            remaining = max(0.0, deadline - monotonic())
            sleep(min(delay, remaining))
            delay = min(delay * 1.5, 30.0)
    if pending:
        raise Metal3SkillError("METAL3_BMH_READY_TIMEOUT:" + ",".join(sorted(pending)))
    inventory_verified = all(
        (
            host.get("hardware", {}).get("system_uuid")
            or host.get("hardware", {}).get("serial_number")
        )
        and isinstance(host.get("hardware", {}).get("nics"), list)
        and bool(host.get("hardware", {}).get("nics"))
        and isinstance(host.get("hardware", {}).get("cpu"), dict)
        and isinstance(host.get("hardware", {}).get("ram_mib"), int)
        for host in observed.values()
    )
    if not inventory_verified:
        raise Metal3SkillError("METAL3_BMH_IRONIC_HARDWARE_INVENTORY_INCOMPLETE")
    return {
        "namespace": namespace,
        "ready_host_names": sorted(observed),
        "ready_host_count": len(observed),
        "expected_bmh_count": len(host_names),
        "bmh_ready_count": len(observed),
        "bmh_all_operational": True,
        "ironic_inventory_verified": True,
        "host_evidence": observed,
        "side_effects": False,
    }


def wait_for_capi_control_plane_ready(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    timeout_seconds = payload.get("timeout_seconds", 1800)
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 3600
    ):
        raise Metal3SkillError("CAPI_READY_TIMEOUT_INVALID")
    deadline = monotonic() + timeout_seconds
    delay = 20.0
    latest = {}
    while monotonic() < deadline:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise Metal3SkillError("COMMAND_LEASE_LOST_DURING_CAPI_WAIT")
        latest = observe_capi_cluster(payload, runner)
        if latest.get("control_plane_ready") is True:
            latest["control_plane_ready_wait_seconds"] = timeout_seconds - max(
                0, int(deadline - monotonic())
            )
            return latest
        if latest.get("failure_reason"):
            raise Metal3SkillError("CAPI_CONTROL_PLANE_FAILED")
        remaining = max(0.0, deadline - monotonic())
        sleep(min(delay, remaining))
        delay = min(delay * 1.5, 60.0)
    raise Metal3SkillError("CAPI_CONTROL_PLANE_READY_TIMEOUT")


def observe_capi_cluster(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    namespace = _namespace(payload.get("namespace", "metal3"))
    cluster_name = payload.get("cluster_name")
    if (
        not isinstance(cluster_name, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", cluster_name)
    ):
        raise Metal3SkillError("CAPI_CLUSTER_NAME_INVALID")
    cluster_result = _invoke(
        runner,
        [
            "get",
            "clusters.cluster.x-k8s.io",
            cluster_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        60,
    )
    try:
        cluster = yaml.safe_load(cluster_result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise Metal3SkillError("CAPI_CLUSTER_STATUS_INVALID") from exc
    if not isinstance(cluster, dict):
        raise Metal3SkillError("CAPI_CLUSTER_STATUS_INVALID")
    metadata = cluster.get("metadata", {})
    status = cluster.get("status", {})
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise Metal3SkillError("CAPI_CLUSTER_STATUS_INVALID")
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(observed_generation, int)
        or isinstance(observed_generation, bool)
        or observed_generation < 0
    ):
        raise Metal3SkillError("CAPI_CLUSTER_GENERATION_INVALID")
    condition_items = status.get("conditions", [])
    if not isinstance(condition_items, list):
        raise Metal3SkillError("CAPI_CLUSTER_CONDITIONS_INVALID")
    conditions = {
        item.get("type"): item.get("status")
        for item in condition_items
        if isinstance(item, dict)
    }
    control_plane_ready = (
        conditions.get("ControlPlaneReady") == "True"
        and observed_generation >= generation
    )
    return {
        "cluster_name": cluster_name,
        "namespace": namespace,
        "cluster_uid": metadata.get("uid"),
        "generation": generation,
        "observed_generation": observed_generation,
        "control_plane_ready": control_plane_ready,
        "conditions": conditions,
        "failure_reason": status.get("failureReason"),
        "failure_message": str(status.get("failureMessage", ""))[:2000],
        "side_effects": False,
    }
