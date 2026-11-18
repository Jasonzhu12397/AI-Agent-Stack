from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Callable

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
BMH_KIND = "BareMetalHost"
FIELD_MANAGER = "ai-agent-eph-agent"
MAX_MANIFESTS = 200
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _namespace(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)
        or len(value) > 63
    ):
        raise Metal3SkillError("METAL3_NAMESPACE_INVALID")
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
    manifests = _validate_bundle(payload.get("manifests"), namespace, CAPI_KINDS)
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
        return {
            "resource_type": resource_prefix,
            "resource_count": len(manifests),
            "resource_names": [item["metadata"]["name"] for item in manifests],
            "server_side_dry_run_passed": True,
            "server_side_apply_succeeded": True,
            "dry_run_output": str(dry_run.get("stdout", ""))[-2000:],
            "apply_output": str(applied.get("stdout", ""))[-4000:],
            "side_effects": True,
        }
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
    status = cluster.get("status", {}) if isinstance(cluster, dict) else {}
    conditions = {
        item.get("type"): item.get("status")
        for item in status.get("conditions", [])
        if isinstance(item, dict)
    }
    control_plane_ready = conditions.get("ControlPlaneReady") == "True"
    return {
        "cluster_name": cluster_name,
        "namespace": namespace,
        "cluster_uid": cluster.get("metadata", {}).get("uid"),
        "observed_generation": status.get("observedGeneration"),
        "control_plane_ready": control_plane_ready,
        "conditions": conditions,
        "failure_reason": status.get("failureReason"),
        "failure_message": str(status.get("failureMessage", ""))[:2000],
        "side_effects": False,
    }
