import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from itertools import count
from pathlib import Path
from typing import Any

from .capi_pivot import CapiPivotError, run_capi_pivot, run_capi_pivot_dry_run


class NodeAgentError(Exception):
    pass


def sanitize_kubernetes_item(resource: str, item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    result = {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
    }
    if resource == "nodes":
        result["conditions"] = [
            {"type": condition.get("type"), "status": condition.get("status")}
            for condition in item.get("status", {}).get("conditions", [])
        ]
        result["capacity"] = item.get("status", {}).get("capacity", {})
        result["allocatable"] = item.get("status", {}).get("allocatable", {})
    elif resource == "namespaces":
        result["phase"] = item.get("status", {}).get("phase")
    elif resource == "pods":
        result["phase"] = item.get("status", {}).get("phase")
        result["node_name"] = item.get("spec", {}).get("nodeName")
    elif resource == "events":
        message = str(item.get("message", ""))[:500]
        result["type"] = item.get("type")
        result["reason"] = item.get("reason")
        result["message"] = re.sub(
            r"(?i)(password|token|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            message,
        )
        result["involved_object"] = {
            "kind": item.get("involvedObject", {}).get("kind"),
            "name": item.get("involvedObject", {}).get("name"),
        }
    elif resource == "services":
        result["type"] = item.get("spec", {}).get("type")
        result["cluster_ip"] = item.get("spec", {}).get("clusterIP")
        result["ports"] = [
            {"port": port.get("port"), "protocol": port.get("protocol")}
            for port in item.get("spec", {}).get("ports", [])
        ]
    return result


def inspect_api_group(
    group_name: str,
    resource_names: list[str],
    namespace: str,
) -> dict[str, Any]:
    if not isinstance(namespace, str) or not namespace or "/" in namespace or ".." in namespace:
        raise NodeAgentError("INVALID_NAMESPACE")
    discovery = kubernetes_api_get("/apis")
    group = next(
        (item for item in discovery.get("groups", []) if item.get("name") == group_name),
        None,
    )
    if group is None:
        return {"api_group": group_name, "discovered": False, "resources": []}
    version = group.get("preferredVersion", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.]+", version):
        raise NodeAgentError("KUBERNETES_API_VERSION_INVALID")
    version_document = kubernetes_api_get(f"/apis/{group_name}/{version}")
    resources = []
    for resource_name in resource_names:
        resource = next(
            (
                item
                for item in version_document.get("resources", [])
                if item.get("name") == resource_name
            ),
            None,
        )
        if resource is None:
            resources.append({"resource": resource_name, "available": False, "items": []})
            continue
        if resource.get("namespaced"):
            path = f"/apis/{group_name}/{version}/namespaces/{namespace}/{resource_name}"
        else:
            path = f"/apis/{group_name}/{version}/{resource_name}"
        document = kubernetes_api_get(path)
        summaries = []
        for item in document.get("items", [])[:100]:
            metadata = item.get("metadata", {})
            summary = {
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
            }
            if group_name == "metal3.io":
                status_data = item.get("status", {})
                summary.update(
                    {
                        "online": item.get("spec", {}).get("online"),
                        "operational_status": status_data.get("operationalStatus"),
                        "provisioning_state": status_data.get("provisioning", {}).get("state"),
                        "powered_on": status_data.get("poweredOn"),
                        "error": str(status_data.get("errorMessage", ""))[:300],
                    }
                )
            else:
                status_data = item.get("status", {})
                summary.update(
                    {
                        "phase": status_data.get("phase"),
                        "version": item.get("spec", {}).get("version"),
                        "node_name": status_data.get("nodeRef", {}).get("name"),
                        "conditions": [
                            {
                                "type": condition.get("type"),
                                "status": condition.get("status"),
                                "reason": condition.get("reason"),
                            }
                            for condition in status_data.get("conditions", [])
                        ],
                    }
                )
            summaries.append(summary)
        resources.append(
            {
                "resource": resource_name,
                "available": True,
                "count": len(document.get("items", [])),
                "items": summaries,
            }
        )
    return {
        "api_group": group_name,
        "discovered": True,
        "version": version,
        "namespace": namespace,
        "resources": resources,
    }


CRD_REQUIREMENTS = {
    "cluster_api": {
        "clusters": "clusters.cluster.x-k8s.io",
        "machines": "machines.cluster.x-k8s.io",
        "machinedeployments": "machinedeployments.cluster.x-k8s.io",
    },
    "bootstrap_kubeadm": {
        "kubeadmconfigs": "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
    },
    "control_plane_kubeadm": {
        "kubeadmcontrolplanes": "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io",
    },
    "infrastructure_metal3": {
        "metal3clusters": "metal3clusters.infrastructure.cluster.x-k8s.io",
        "metal3machines": "metal3machines.infrastructure.cluster.x-k8s.io",
        "metal3machinetemplates": "metal3machinetemplates.infrastructure.cluster.x-k8s.io",
    },
    "metal3": {
        "baremetalhosts": "baremetalhosts.metal3.io",
    },
}


def summarize_crd_schema(crd: dict[str, Any]) -> dict[str, Any]:
    spec = crd.get("spec", {})
    served_versions = []
    errors = []
    for version in spec.get("versions", []):
        if not isinstance(version, dict) or version.get("served") is not True:
            continue
        version_schema = version.get("schema", {})
        schema = (
            version_schema.get("openAPIV3Schema")
            if isinstance(version_schema, dict)
            else None
        )
        if not isinstance(schema, dict):
            errors.append("served_version_missing_openapi_schema")
            continue
        properties = schema.get("properties", {})
        spec_schema = (
            properties.get("spec", {})
            if isinstance(properties, dict)
            else {}
        )
        spec_properties = (
            spec_schema.get("properties", {})
            if isinstance(spec_schema, dict)
            else {}
        )
        required = (
            spec_schema.get("required", [])
            if isinstance(spec_schema, dict)
            else []
        )
        served_versions.append(
            {
                "name": version.get("name"),
                "storage": version.get("storage") is True,
                "spec_properties": sorted(
                    key for key in spec_properties if isinstance(key, str)
                )[:100],
                "required_spec_fields": sorted(
                    key for key in required if isinstance(key, str)
                )[:100],
            }
        )
    if not served_versions:
        errors.append("no_served_version_with_openapi_schema")
    names = spec.get("names", {})
    return {
        "crd_name": names.get("plural", "") + "." + spec.get("group", ""),
        "group": spec.get("group"),
        "kind": names.get("kind"),
        "scope": spec.get("scope"),
        "served_versions": served_versions,
        "schema_valid": not errors,
        "schema_errors": errors,
    }



def validate_openapi_value(
    value: Any,
    schema: dict[str, Any],
    path: str,
    errors: list[str],
    depth: int = 0,
) -> None:
    if depth > 64:
        errors.append(path + ":maximum_validation_depth_exceeded")
        return
    if not isinstance(schema, dict):
        errors.append(path + ":schema_malformed")
        return
    expected = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected in type_checks and not type_checks[expected](value):
        errors.append(path + ":type_mismatch")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(path + ":enum_mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(path + ":below_minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(path + ":above_maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(path + ":string_too_short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(path + ":string_too_long")
        if "pattern" in schema:
            try:
                if re.search(schema["pattern"], value) is None:
                    errors.append(path + ":pattern_mismatch")
            except re.error:
                errors.append(path + ":schema_pattern_invalid")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(path + ":too_few_items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(path + ":too_many_items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value[:1000]):
                validate_openapi_value(item, item_schema, f"{path}[{index}]", errors, depth + 1)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            errors.append(path + ":schema_object_invalid")
            return
        for field in required:
            if isinstance(field, str) and field not in value:
                errors.append(path + "." + field + ":required")
        for field, child in value.items():
            child_schema = properties.get(field)
            if isinstance(child_schema, dict):
                validate_openapi_value(child, child_schema, path + "." + field, errors, depth + 1)
            elif schema.get("additionalProperties") is False:
                errors.append(path + "." + str(field) + ":unknown_field")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_openapi_value(
                    child,
                    schema["additionalProperties"],
                    path + "." + str(field),
                    errors,
                    depth + 1,
                )


def validate_provider_manifests(manifests: Any) -> dict[str, Any]:
    if not isinstance(manifests, list) or not manifests or len(manifests) > 100:
        raise NodeAgentError("INVALID_MANIFEST_LIST")
    try:
        encoded_size = len(
            json.dumps(manifests, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise NodeAgentError("INVALID_MANIFEST_LIST") from exc
    if encoded_size > 1024 * 1024:
        raise NodeAgentError("MANIFEST_PAYLOAD_TOO_LARGE")
    crd_cache = {}
    results = []
    for index, manifest in enumerate(manifests):
        if not isinstance(manifest, dict):
            raise NodeAgentError("INVALID_MANIFEST_OBJECT")
        api_version = manifest.get("apiVersion")
        kind = manifest.get("kind")
        metadata = manifest.get("metadata")
        if (
            not isinstance(api_version, str)
            or "/" not in api_version
            or not isinstance(kind, str)
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("name"), str)
            or not metadata.get("name")
        ):
            raise NodeAgentError("INVALID_MANIFEST_IDENTITY")
        group, version_name = api_version.split("/", 1)
        matched = None
        for provider_resources in CRD_REQUIREMENTS.values():
            for crd_name in provider_resources.values():
                if crd_name not in crd_cache:
                    try:
                        crd_cache[crd_name] = kubernetes_api_get(
                            "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/" + crd_name
                        )
                    except NodeAgentError as exc:
                        if str(exc) == "KUBERNETES_API_HTTP_404":
                            crd_cache[crd_name] = None
                        else:
                            raise
                crd = crd_cache[crd_name]
                if not isinstance(crd, dict):
                    continue
                crd_spec = crd.get("spec", {})
                names = crd_spec.get("names", {})
                if crd_spec.get("group") != group or names.get("kind") != kind:
                    continue
                version_entry = next(
                    (
                        item for item in crd_spec.get("versions", [])
                        if isinstance(item, dict)
                        and item.get("name") == version_name
                        and item.get("served") is True
                    ),
                    None,
                )
                if version_entry is None:
                    matched = {"error": "unsupported_api_version"}
                    break
                openapi = version_entry.get("schema", {}).get("openAPIV3Schema")
                matched = {
                    "schema": openapi,
                    "scope": crd_spec.get("scope"),
                    "error": None if isinstance(openapi, dict) else "openapi_schema_missing",
                }
                break
            if matched is not None:
                break
        if matched is None:
            results.append({
                "index": index,
                "valid": False,
                "errors": ["crd_not_found_for_group_and_kind"],
            })
            continue
        if matched.get("error"):
            results.append({
                "index": index,
                "valid": False,
                "errors": [matched["error"]],
            })
            continue
        errors = []
        if matched["scope"] == "Namespaced" and not metadata.get("namespace"):
            errors.append("metadata.namespace:required_for_namespaced_resource")
        if matched["scope"] == "Cluster" and metadata.get("namespace"):
            errors.append("metadata.namespace:not_allowed_for_cluster_scoped_resource")
        validate_openapi_value(manifest, matched["schema"], f"manifests[{index}]", errors)
        results.append({"index": index, "valid": not errors, "errors": errors[:100]})
    return {
        "validated_count": len(results),
        "valid": all(item["valid"] for item in results),
        "results": results,
        "validation_mode": "read_only_live_crd_openapi_schema",
        "server_side_dry_run": False,
        "mutation_adapter_enabled": False,
        "provisioning_ready": False,
    }


def inspect_provider_crd_schemas(providers: dict[str, Any]) -> dict[str, Any]:
    results = {}
    missing = []
    invalid = []
    for provider, resources in CRD_REQUIREMENTS.items():
        available = providers.get(provider, {}).get("resources", {})
        for resource, crd_name in resources.items():
            if available.get(resource) is not True:
                continue
            path = (
                "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/"
                + crd_name
            )
            try:
                crd = kubernetes_api_get(path)
            except NodeAgentError as exc:
                if str(exc) == "KUBERNETES_API_HTTP_404":
                    missing.append(crd_name)
                    results[crd_name] = {
                        "found": False,
                        "schema_valid": False,
                    }
                    continue
                raise
            summary = summarize_crd_schema(crd)
            summary["found"] = True
            results[crd_name] = summary
            if not summary["schema_valid"]:
                invalid.append(crd_name)
    return {
        "crds": results,
        "missing_crds": missing,
        "invalid_schemas": invalid,
        "schema_check_complete": not missing and not invalid,
    }


def discover_provider_apis() -> dict[str, Any]:
    requirements = {
        "cluster_api": ("cluster.x-k8s.io", ("clusters", "machines", "machinedeployments")),
        "bootstrap_kubeadm": ("bootstrap.cluster.x-k8s.io", ("kubeadmconfigs",)),
        "control_plane_kubeadm": ("controlplane.cluster.x-k8s.io", ("kubeadmcontrolplanes",)),
        "infrastructure_metal3": (
            "infrastructure.cluster.x-k8s.io",
            ("metal3clusters", "metal3machines", "metal3machinetemplates"),
        ),
        "metal3": ("metal3.io", ("baremetalhosts",)),
    }
    discovery = kubernetes_api_get("/apis")
    groups = {
        item.get("name"): item
        for item in discovery.get("groups", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    providers = {}
    missing = []
    for provider, (group_name, required_resources) in requirements.items():
        group = groups.get(group_name)
        if group is None:
            providers[provider] = {
                "api_group": group_name,
                "discovered": False,
                "version": None,
                "resources": {},
            }
            missing.append(f"provider_api.{provider}")
            continue
        version = group.get("preferredVersion", {}).get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.]+", version):
            raise NodeAgentError("KUBERNETES_API_VERSION_INVALID")
        version_document = kubernetes_api_get(f"/apis/{group_name}/{version}")
        available = {
            item.get("name"): item
            for item in version_document.get("resources", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        resource_status = {
            name: name in available
            for name in required_resources
        }
        providers[provider] = {
            "api_group": group_name,
            "discovered": True,
            "version": version,
            "resources": resource_status,
        }
        for resource_name, is_available in resource_status.items():
            if not is_available:
                missing.append(f"provider_api.{provider}.{resource_name}")
    schema_inspection = inspect_provider_crd_schemas(providers)
    return {
        "management_kubernetes_version": kubernetes_api_get("/version").get("gitVersion"),
        "providers": providers,
        "missing_requirements": missing,
        "discovery_complete": not missing,
        "crd_schema_inspection": schema_inspection,
        "external_components": {
            "ironic": "not_verified_by_kubernetes_api_discovery",
            "ipa": "not_verified_by_kubernetes_api_discovery",
            "bmc_connectivity": "not_verified",
            "machine_image": "not_verified",
        },
        "crd_schema_validation": "read_only_openapi_schema_summary",
        "manifest_generation_enabled": False,
        "mutation_adapter_enabled": False,
        "provisioning_ready": False,
    }


def canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contains_forbidden_fields(value: Any) -> bool:
    forbidden = {
        "password",
        "secret",
        "token",
        "private_key",
        "bmc_password",
        "bmc_username",
        "kubeconfig",
        "raw_manifest",
        "shell",
        "script",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or contains_forbidden_fields(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(contains_forbidden_fields(child) for child in value)
    return False



def validate_network_inputs(network: Any) -> None:
    if not isinstance(network, dict):
        return
    cidr_keys = ("pod_cidr", "service_cidr")
    if any(network.get(key) is not None for key in cidr_keys):
        for key in cidr_keys:
            value = network.get(key)
            if not isinstance(value, str) or not value.strip():
                raise NodeAgentError("INVALID_NETWORK_CIDR")
            try:
                ipaddress.ip_network(value, strict=False)
            except ValueError as exc:
                raise NodeAgentError("INVALID_NETWORK_CIDR") from exc
    endpoint = network.get("control_plane_endpoint")
    if endpoint:
        if not isinstance(endpoint, str) or ":" not in endpoint:
            raise NodeAgentError("INVALID_CONTROL_PLANE_ENDPOINT")
        host, separator, port_text = endpoint.rpartition(":")
        if (
            not separator
            or not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])", host)
            or not port_text.isdigit()
        ):
            raise NodeAgentError("INVALID_CONTROL_PLANE_ENDPOINT")
        if not 1 <= int(port_text) <= 65535:
            raise NodeAgentError("INVALID_CONTROL_PLANE_ENDPOINT")


def validate_metal3_hosts(hosts: Any) -> None:
    if not isinstance(hosts, list):
        return
    host_names = set()
    boot_macs = set()
    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_name = host.get("baremetal_host")
        if host_name:
            if not isinstance(host_name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,252}",
                host_name,
            ):
                raise NodeAgentError("INVALID_BAREMETAL_HOST_REFERENCE")
            if host_name in host_names:
                raise NodeAgentError("DUPLICATE_BAREMETAL_HOST")
            host_names.add(host_name)
        boot_mac = host.get("boot_mac")
        if boot_mac:
            normalized_mac = str(boot_mac).lower().replace("-", ":")
            if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized_mac):
                raise NodeAgentError("INVALID_BOOT_MAC")
            if normalized_mac in boot_macs:
                raise NodeAgentError("DUPLICATE_BOOT_MAC")
            boot_macs.add(normalized_mac)


CAPI_PIVOT_TRACKED_OWNER_KINDS = {
    "Cluster",
    "Machine",
    "MachineSet",
    "MachineDeployment",
    "KubeadmConfig",
    "KubeadmConfigTemplate",
    "KubeadmControlPlane",
    "Metal3Cluster",
    "Metal3Machine",
    "Metal3MachineTemplate",
    "BareMetalHost",
}


def list_all_kubernetes_items(path: str, max_items: int = 100000) -> list[dict[str, Any]]:
    items = []
    continuation = ""
    seen_tokens = set()
    for _ in range(201):
        query = {"limit": "500"}
        if continuation:
            query["continue"] = continuation
        request_path = f"{path}?{urllib.parse.urlencode(query)}"
        document = kubernetes_api_get(request_path)
        page_items = document.get("items", [])
        metadata = document.get("metadata", {})
        if not isinstance(page_items, list) or not isinstance(metadata, dict):
            raise NodeAgentError("KUBERNETES_LIST_RESPONSE_INVALID")
        if any(not isinstance(item, dict) for item in page_items):
            raise NodeAgentError("KUBERNETES_LIST_ITEM_INVALID")
        items.extend(page_items)
        if len(items) > max_items:
            raise NodeAgentError("KUBERNETES_INVENTORY_LIMIT_EXCEEDED")
        next_token = metadata.get("continue", "")
        if not isinstance(next_token, str):
            raise NodeAgentError("KUBERNETES_CONTINUE_TOKEN_INVALID")
        if not next_token:
            return items
        if next_token in seen_tokens:
            raise NodeAgentError("KUBERNETES_CONTINUE_TOKEN_REPEATED")
        seen_tokens.add(next_token)
        continuation = next_token
    raise NodeAgentError("KUBERNETES_INVENTORY_PAGE_LIMIT_EXCEEDED")


def build_capi_owner_graph(
    inventory: list[dict[str, Any]],
    cluster_name: str,
    cluster_uid: str,
    inventory_complete: bool,
) -> dict[str, Any]:
    uid_map = {}
    duplicate_uids = []
    unresolved_owner_references = []
    truncated_owner_references = []
    children_by_owner = {}
    for item in inventory:
        uid = item.get("uid")
        if not isinstance(uid, str) or not uid:
            duplicate_uids.append("missing_uid")
            continue
        if uid in uid_map:
            duplicate_uids.append(uid)
            continue
        uid_map[uid] = item
        if item.get("owner_references_truncated") is True:
            truncated_owner_references.append(uid)

    root = uid_map.get(cluster_uid)
    root_valid = (
        isinstance(root, dict)
        and root.get("kind") == "Cluster"
        and root.get("name") == cluster_name
        and root.get("resource") == "cluster.x-k8s.io/clusters"
    )
    reachable = {cluster_uid} if root_valid else set()

    for item in inventory:
        owner_references = item.get("owner_references", [])
        if not isinstance(owner_references, list):
            unresolved_owner_references.append(
                {"uid": item.get("uid"), "reason": "owner_references_invalid"}
            )
            continue
        for owner in owner_references:
            if not isinstance(owner, dict):
                unresolved_owner_references.append(
                    {"uid": item.get("uid"), "reason": "owner_reference_invalid"}
                )
                continue
            owner_uid = owner.get("uid")
            child_uid = item.get("uid")
            if isinstance(owner_uid, str) and isinstance(child_uid, str):
                children_by_owner.setdefault(owner_uid, []).append(child_uid)
            if owner.get("kind") not in CAPI_PIVOT_TRACKED_OWNER_KINDS:
                continue
            owner_item = uid_map.get(owner_uid)
            if owner_item is None:
                unresolved_owner_references.append(
                    {
                        "uid": item.get("uid"),
                        "owner_uid": owner_uid,
                        "owner_kind": owner.get("kind"),
                        "reason": "tracked_owner_missing",
                    }
                )
            elif (
                owner_item.get("kind") != owner.get("kind")
                or owner_item.get("name") != owner.get("name")
                or owner_item.get("api_version") != owner.get("api_version")
            ):
                unresolved_owner_references.append(
                    {
                        "uid": item.get("uid"),
                        "owner_uid": owner_uid,
                        "owner_kind": owner.get("kind"),
                        "reason": "tracked_owner_identity_mismatch",
                    }
                )

    pending = [cluster_uid] if root_valid else []
    for owner_uid in pending:
        for child_uid in children_by_owner.get(owner_uid, []):
            if child_uid not in reachable:
                reachable.add(child_uid)
                pending.append(child_uid)

    cluster_labeled_objects = [
        item
        for item in inventory
        if item.get("cluster_name") == cluster_name
    ]
    orphaned_cluster_objects = [
        {
            "resource": item.get("resource"),
            "namespace": item.get("namespace"),
            "name": item.get("name"),
            "uid": item.get("uid"),
        }
        for item in cluster_labeled_objects
        if item.get("uid") not in reachable
    ]
    complete = (
        inventory_complete
        and root_valid
        and len(uid_map) == len(inventory)
        and not duplicate_uids
        and not unresolved_owner_references
        and not truncated_owner_references
        and not orphaned_cluster_objects
    )
    return {
        "complete": complete,
        "root_cluster_uid": cluster_uid if root_valid else None,
        "object_count": len(inventory),
        "unique_uid_count": len(uid_map),
        "reachable_object_count": len(reachable),
        "cluster_labeled_object_count": len(cluster_labeled_objects),
        "duplicate_uids": duplicate_uids[:100],
        "truncated_owner_references": truncated_owner_references[:100],
        "unresolved_owner_references": unresolved_owner_references[:100],
        "orphaned_cluster_objects": orphaned_cluster_objects[:100],
    }



def validate_network_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise NodeAgentError("INVALID_NETWORK_PROFILE")
    if profile.get("version") != "v1":
        raise NodeAgentError("UNSUPPORTED_NETWORK_PROFILE_VERSION")
    control_plane = profile.get("control_plane")
    data_plane = profile.get("data_plane")
    if not isinstance(control_plane, dict) or not isinstance(data_plane, dict):
        raise NodeAgentError("NETWORK_PROFILE_SEGMENTS_REQUIRED")
    interface = control_plane.get("interface")
    cidr = control_plane.get("cidr")
    gateway = control_plane.get("gateway")
    if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", interface):
        raise NodeAgentError("INVALID_CONTROL_PLANE_INTERFACE")
    try:
        control_network = ipaddress.ip_interface(cidr)
        control_gateway = ipaddress.ip_address(gateway)
    except (TypeError, ValueError) as exc:
        raise NodeAgentError("INVALID_CONTROL_PLANE_ADDRESSING") from exc
    if (
        control_network.version != control_gateway.version
        or control_gateway not in control_network.network
    ):
        raise NodeAgentError("CONTROL_PLANE_GATEWAY_OUTSIDE_CIDR")
    control_mtu = control_plane.get("mtu")
    if (
        not isinstance(control_mtu, int)
        or isinstance(control_mtu, bool)
        or not 576 <= control_mtu <= 9216
    ):
        raise NodeAgentError("INVALID_CONTROL_PLANE_MTU")
    interfaces = data_plane.get("interfaces")
    if not isinstance(interfaces, list) or not interfaces:
        raise NodeAgentError("DATA_PLANE_INTERFACES_REQUIRED")
    if any(
        not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item)
        for item in interfaces
    ):
        raise NodeAgentError("INVALID_DATA_PLANE_INTERFACE")
    if len(set(interfaces)) != len(interfaces) or interface in interfaces:
        raise NodeAgentError("NETWORK_INTERFACE_CONFLICT")
    data_mtu = data_plane.get("mtu")
    if not isinstance(data_mtu, int) or isinstance(data_mtu, bool) or not 576 <= data_mtu <= 9216:
        raise NodeAgentError("INVALID_DATA_PLANE_MTU")
    if profile.get("pod_traffic_default") != "data_plane":
        raise NodeAgentError("POD_TRAFFIC_MUST_DEFAULT_TO_DATA_PLANE")
    cni = data_plane.get("cni")
    if not isinstance(cni, dict) or cni.get("primary") != "calico" or cni.get("meta") != "multus":
        raise NodeAgentError("UNSUPPORTED_PRIMARY_CNI_COMPOSITION")
    attachments = cni.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 32:
        raise NodeAgentError("INVALID_NETWORK_ATTACHMENTS")
    names = set()
    dpdk_requested = False
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise NodeAgentError("INVALID_NETWORK_ATTACHMENT")
        name = attachment.get("name")
        kind = attachment.get("type")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", name)
            or name in names
        ):
            raise NodeAgentError("INVALID_NETWORK_ATTACHMENT_NAME")
        names.add(name)
        if kind not in {"ovs", "sriov"}:
            raise NodeAgentError("UNSUPPORTED_NETWORK_ATTACHMENT_TYPE")
        if kind == "sriov" and (
            not isinstance(attachment.get("resource_name"), str)
            or not re.fullmatch(r"[A-Za-z0-9./_-]{1,128}", attachment["resource_name"])
        ):
            raise NodeAgentError("SRIOV_RESOURCE_NAME_REQUIRED")
        if attachment.get("dpdk") is True:
            dpdk_requested = True
            if kind != "sriov":
                raise NodeAgentError("DPDK_REQUIRES_SRIOV_ATTACHMENT")
            if not data_plane.get("hugepages"):
                raise NodeAgentError("DPDK_HUGEPAGES_REQUIRED")
            if not isinstance(data_plane.get("numa_policy"), str) or not data_plane["numa_policy"]:
                raise NodeAgentError("DPDK_NUMA_POLICY_REQUIRED")
            if data_plane.get("cpu_manager_policy") != "static":
                raise NodeAgentError("DPDK_CPU_MANAGER_STATIC_REQUIRED")
    return {
        "valid": True,
        "pod_traffic_default": "data_plane",
        "primary_cni": "calico",
        "meta_cni": "multus",
        "attachment_count": len(attachments),
        "dpdk_requested": dpdk_requested,
        "profile_hash": canonical_hash(profile),
    }


def _pod_network_namespace_snapshot() -> dict[str, Any]:
    interfaces = []
    try:
        interfaces = sorted(
            item.name
            for item in Path("/sys/class/net").iterdir()
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item.name)
        )
    except OSError:
        interfaces = []
    default_routes = []
    try:
        with open("/proc/net/route", encoding="ascii") as route_file:
            for line in route_file.readlines()[1:]:
                fields = line.split()
                if len(fields) < 8 or fields[1] != "00000000":
                    continue
                try:
                    gateway = str(ipaddress.IPv4Address(
                        int.from_bytes(bytes.fromhex(fields[2]), "little")
                    ))
                    default_routes.append({"interface": fields[0], "gateway": gateway})
                except (ValueError, OSError):
                    continue
    except OSError:
        pass
    return {
        "scope": "agent_pod_network_namespace",
        "interfaces": interfaces[:64],
        "default_routes": default_routes[:16],
    }



def _read_sysfs_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value[:256]


def inspect_host_network_hardware() -> dict[str, Any]:
    configured_root = os.environ.get("AGENT_HOST_SYSFS_PATH", "").strip()
    if not configured_root:
        return {
            "status": "not_configured",
            "scope": "host_sysfs",
            "reason": "AGENT_HOST_SYSFS_PATH is not mounted",
        }
    root = Path(configured_root)
    net_root = root / "class" / "net"
    pci_root = root / "bus" / "pci" / "devices"
    if not net_root.is_dir() or not pci_root.is_dir():
        return {
            "status": "unavailable",
            "scope": "host_sysfs",
            "reason": "required host sysfs paths are not readable",
        }
    interfaces = []
    try:
        net_entries = sorted(net_root.iterdir(), key=lambda item: item.name)
    except OSError:
        net_entries = []
    for interface in net_entries[:256]:
        device = interface / "device"
        pci_address = None
        driver = None
        try:
            if device.exists():
                pci_address = device.resolve().name
                driver_link = device / "driver"
                if driver_link.is_symlink():
                    driver = os.path.basename(os.readlink(driver_link))
        except OSError:
            pass
        interfaces.append({
            "name": interface.name,
            "mac_address": _read_sysfs_text(interface / "address"),
            "mtu": _read_sysfs_text(interface / "mtu"),
            "operstate": _read_sysfs_text(interface / "operstate"),
            "pci_address": pci_address,
            "driver": driver,
            "numa_node": _read_sysfs_text(device / "numa_node"),
            "sriov_total_vfs": _read_sysfs_text(device / "sriov_totalvfs"),
            "sriov_enabled_vfs": _read_sysfs_text(device / "sriov_numvfs"),
        })
    pci_devices = []
    try:
        pci_entries = sorted(pci_root.iterdir(), key=lambda item: item.name)
    except OSError:
        pci_entries = []
    for device in pci_entries[:4096]:
        net_directory = device / "net"
        try:
            network_interfaces = sorted(item.name for item in net_directory.iterdir())
        except OSError:
            network_interfaces = []
        virtual_function_links = list(device.glob("virtfn*"))
        is_virtual_function = (device / "physfn").exists()
        if not network_interfaces and not is_virtual_function and not virtual_function_links:
            continue
        driver = None
        try:
            driver_link = device / "driver"
            if driver_link.is_symlink():
                driver = os.path.basename(os.readlink(driver_link))
        except OSError:
            pass
        pci_devices.append({
            "pci_address": device.name,
            "vendor": _read_sysfs_text(device / "vendor"),
            "device": _read_sysfs_text(device / "device"),
            "class": _read_sysfs_text(device / "class"),
            "driver": driver,
            "numa_node": _read_sysfs_text(device / "numa_node"),
            "network_interfaces": network_interfaces[:32],
            "iommu_group_present": (device / "iommu_group").exists(),
            "is_virtual_function": is_virtual_function,
            "virtual_function_count": len(virtual_function_links),
        })
    hugepages = []
    hugepage_root = root / "kernel" / "mm" / "hugepages"
    try:
        hugepage_sizes = sorted(hugepage_root.glob("hugepages-*"))
    except OSError:
        hugepage_sizes = []
    for page_size in hugepage_sizes[:16]:
        hugepages.append({
            "page_size": page_size.name,
            "total": _read_sysfs_text(page_size / "nr_hugepages"),
            "free": _read_sysfs_text(page_size / "free_hugepages"),
        })
    numa_nodes = []
    node_root = root / "devices" / "system" / "node"
    try:
        node_entries = sorted(node_root.glob("node[0-9]*"))
    except OSError:
        node_entries = []
    for node in node_entries[:128]:
        node_hugepages = []
        try:
            page_entries = sorted((node / "hugepages").glob("hugepages-*"))
        except OSError:
            page_entries = []
        for page_size in page_entries[:16]:
            node_hugepages.append({
                "page_size": page_size.name,
                "total": _read_sysfs_text(page_size / "nr_hugepages"),
                "free": _read_sysfs_text(page_size / "free_hugepages"),
            })
        numa_nodes.append({
            "node": node.name,
            "cpulist": _read_sysfs_text(node / "cpulist"),
            "hugepages": node_hugepages,
        })
    return {
        "status": "observed",
        "scope": "host_sysfs_read_only",
        "cpu_online": _read_sysfs_text(root / "devices" / "system" / "cpu" / "online"),
        "interfaces": interfaces,
        "pci_network_devices": pci_devices,
        "hugepages": hugepages,
        "numa_nodes": numa_nodes,
        "inventory_truncated": (
            len(net_entries) > 256
            or len(pci_entries) > 4096
            or len(node_entries) > 128
        ),
    }



def inspect_cluster_node_hardware_inventory(
    cluster_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        configmaps = list_all_kubernetes_items(
            "/api/v1/namespaces/ai-agent-inventory/configmaps",
            max_items=5000,
        )
    except NodeAgentError as exc:
        return {
            "status": "unavailable",
            "error_code": str(exc),
            "nodes": [],
        }
    expected_node_uids = {
        item.get("metadata", {}).get("name"): item.get("metadata", {}).get("uid")
        for item in (cluster_nodes or [])
        if item.get("metadata", {}).get("name") and item.get("metadata", {}).get("uid")
    }
    reports = []
    stale_node_names = []
    for configmap in configmaps:
        labels = configmap.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/name") != "network-inventory-agent":
            continue
        data = configmap.get("data", {})
        raw_report = data.get("inventory.json") if isinstance(data, dict) else None
        try:
            report = json.loads(raw_report) if isinstance(raw_report, str) else {}
        except json.JSONDecodeError:
            report = {}
        if not isinstance(report, dict):
            continue
        collected_at = report.get("collected_at")
        try:
            collected_time = datetime.datetime.fromisoformat(collected_at)
            if collected_time.tzinfo is None:
                raise ValueError("timezone_required")
            report_age = (
                datetime.datetime.now(datetime.UTC) - collected_time
            ).total_seconds()
        except (TypeError, ValueError):
            stale_node_names.append(report.get("node_name"))
            continue
        if report_age < -60 or report_age > 180:
            stale_node_names.append(report.get("node_name"))
            continue
        hardware = report.get("hardware", {})
        if (
            report.get("schema_version") != "v1"
            or not isinstance(report.get("node_name"), str)
            or not isinstance(report.get("node_uid"), str)
            or not isinstance(hardware, dict)
            or hardware.get("status") != "observed"
            or (
                cluster_nodes is not None
                and expected_node_uids.get(report.get("node_name")) != report.get("node_uid")
            )
        ):
            continue
        reports.append({
            "node_name": report["node_name"],
            "node_uid": report["node_uid"],
            "collected_at": report.get("collected_at"),
            "cpu_online": hardware.get("cpu_online"),
            "interfaces": hardware.get("interfaces", [])[:256],
            "pci_network_devices": hardware.get("pci_network_devices", [])[:4096],
            "hugepages": hardware.get("hugepages", [])[:16],
            "numa_nodes": hardware.get("numa_nodes", [])[:128],
        })
    reports.sort(key=lambda item: item["node_name"])
    reported_names = {item["node_name"] for item in reports}
    expected_names = set(expected_node_uids)
    return {
        "status": "observed",
        "node_count": len(reports),
        "expected_node_count": len(expected_node_uids),
        "inventory_complete": bool(expected_node_uids) and expected_names.issubset(reported_names),
        "missing_node_names": sorted(expected_names - reported_names)[:1000],
        "stale_node_names": sorted(set(name for name in stale_node_names if name))[:1000],
        "nodes": reports[:1000],
        "truncated": len(reports) > 1000,
    }


def inspect_network_environment(
    payload: dict[str, Any] | None = None,
    node_type: str = "",
) -> dict[str, Any]:
    payload = payload or {}
    workload_namespaces = payload.get("workload_namespaces", [])
    expected_attachments = payload.get("expected_attachments", [])
    expected_default_network = payload.get("expected_default_network")
    require_data_plane_default = payload.get("require_data_plane_default", False)
    expected_agent_default_interface = payload.get("expected_agent_default_interface")
    expected_agent_default_gateway = payload.get("expected_agent_default_gateway")
    if expected_agent_default_interface is not None and (
        not isinstance(expected_agent_default_interface, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", expected_agent_default_interface)
    ):
        raise NodeAgentError("INVALID_EXPECTED_AGENT_DEFAULT_INTERFACE")
    if expected_agent_default_gateway is not None:
        if not isinstance(expected_agent_default_gateway, str):
            raise NodeAgentError("INVALID_EXPECTED_AGENT_DEFAULT_GATEWAY")
        try:
            ipaddress.ip_address(expected_agent_default_gateway)
        except ValueError as exc:
            raise NodeAgentError("INVALID_EXPECTED_AGENT_DEFAULT_GATEWAY") from exc
        if expected_agent_default_interface is None:
            raise NodeAgentError("AGENT_DEFAULT_INTERFACE_REQUIRED")
    if not isinstance(require_data_plane_default, bool):
        raise NodeAgentError("INVALID_DATA_PLANE_DEFAULT_REQUIREMENT")
    if expected_default_network is not None and (
        not isinstance(expected_default_network, str)
        or not re.fullmatch(
            r"(?:[a-z0-9](?:[-a-z0-9]*[a-z0-9])?/)?[a-z0-9](?:[-a-z0-9]*[a-z0-9])?",
            expected_default_network,
        )
    ):
        raise NodeAgentError("INVALID_EXPECTED_DEFAULT_NETWORK")
    if require_data_plane_default and not expected_default_network:
        raise NodeAgentError("EXPECTED_DEFAULT_NETWORK_REQUIRED")
    if (
        not isinstance(workload_namespaces, list)
        or len(workload_namespaces) > 32
        or any(
            not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
            for namespace in workload_namespaces
        )
        or len(set(workload_namespaces)) != len(workload_namespaces)
    ):
        raise NodeAgentError("INVALID_WORKLOAD_NAMESPACES")
    if (
        not isinstance(expected_attachments, list)
        or len(expected_attachments) > 32
        or any(
            not isinstance(name, str)
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[-a-z0-9]*[a-z0-9])?/)?[a-z0-9](?:[-a-z0-9]*[a-z0-9])?",
                name,
            )
            for name in expected_attachments
        )
        or len(set(expected_attachments)) != len(expected_attachments)
    ):
        raise NodeAgentError("INVALID_EXPECTED_NETWORK_ATTACHMENTS")
    if expected_attachments and not workload_namespaces:
        raise NodeAgentError("WORKLOAD_NAMESPACES_REQUIRED_FOR_ATTACHMENT_CHECK")
    if require_data_plane_default and not workload_namespaces:
        raise NodeAgentError("WORKLOAD_NAMESPACES_REQUIRED_FOR_DEFAULT_NETWORK_CHECK")
    version = kubernetes_api_get("/version")
    nodes = list_all_kubernetes_items("/api/v1/nodes", max_items=5000)
    pods = list_all_kubernetes_items("/api/v1/namespaces/kube-system/pods", max_items=5000)
    api_groups = kubernetes_api_get("/apis").get("groups", [])
    group = next(
        (item for item in api_groups if item.get("name") == "k8s.cni.cncf.io"),
        None,
    )
    network_attachments = []
    if isinstance(group, dict):
        api_version = group.get("preferredVersion", {}).get("version")
        if isinstance(api_version, str) and re.fullmatch(r"[A-Za-z0-9.]+", api_version):
            documents = list_all_kubernetes_items(
                f"/apis/k8s.cni.cncf.io/{api_version}/network-attachment-definitions",
                max_items=5000,
            )
            for item in documents:
                metadata = item.get("metadata", {})
                config_text = item.get("spec", {}).get("config", "")
                plugin_types = []
                try:
                    config = json.loads(config_text) if isinstance(config_text, str) else {}
                except json.JSONDecodeError:
                    config = {}
                pending = [config] if isinstance(config, dict) else []
                while pending:
                    current = pending.pop()
                    plugin_type = current.get("type")
                    if isinstance(plugin_type, str) and plugin_type not in plugin_types:
                        plugin_types.append(plugin_type)
                    plugins = current.get("plugins", [])
                    if isinstance(plugins, list):
                        pending.extend(plugin for plugin in plugins if isinstance(plugin, dict))
                network_attachments.append({
                    "name": metadata.get("name"),
                    "namespace": metadata.get("namespace"),
                    "plugin_types": sorted(plugin_types)[:16],
                })
    kube_system_pods = [
        item for item in pods
        if item.get("metadata", {}).get("namespace") == "kube-system"
    ]
    component_patterns = {
        "calico": ("calico-node", "calico-kube-controllers"),
        "multus": ("multus",),
        "ovs": ("ovs", "openvswitch"),
        "sriov_device_plugin": ("sriov-device-plugin", "sriovdp"),
    }
    observed_components = {}
    pod_names = [
        item.get("metadata", {}).get("name", "")
        for item in kube_system_pods
    ]
    for component, patterns in component_patterns.items():
        observed_components[component] = any(
            pattern in name.lower()
            for name in pod_names
            for pattern in patterns
        )
    node_network_resources = []
    for node in nodes:
        metadata = node.get("metadata", {})
        allocatable = node.get("status", {}).get("allocatable", {})
        extended = {
            key: value for key, value in allocatable.items()
            if "/" in key or "hugepages-" in key
        }
        node_network_resources.append({
            "node": metadata.get("name"),
            "labels": {
                key: value for key, value in metadata.get("labels", {}).items()
                if any(token in key.lower() for token in ("sriov", "dpdk", "network", "numa"))
            },
            "allocatable_network_resources": extended,
        })
    plugin_types = sorted({
        plugin
        for attachment in network_attachments
        for plugin in attachment["plugin_types"]
    })
    available_networks = {
        f"{item.get('namespace')}/{item.get('name')}"
        for item in network_attachments
        if item.get("namespace") and item.get("name")
    }
    available_network_names = {
        item.get("name") for item in network_attachments if item.get("name")
    }
    missing_network_definitions = [
        name for name in expected_attachments
        if name not in available_networks
        and name.split("/", 1)[-1] not in available_network_names
    ]
    workload_results = []
    for namespace in workload_namespaces:
        try:
            workload_pods = list_all_kubernetes_items(
                f"/api/v1/namespaces/{namespace}/pods",
                max_items=5000,
            )
        except NodeAgentError as exc:
            workload_results.append({
                "namespace": namespace,
                "inspection_status": "unavailable",
                "error_code": str(exc),
            })
            continue
        pod_results = []
        for pod in workload_pods:
            metadata = pod.get("metadata", {})
            annotations = metadata.get("annotations", {})
            if not isinstance(annotations, dict):
                annotations = {}
            raw_status = annotations.get("k8s.v1.cni.cncf.io/network-status", "[]")
            try:
                status_items = json.loads(raw_status) if isinstance(raw_status, str) else []
            except json.JSONDecodeError:
                status_items = []
            if not isinstance(status_items, list):
                status_items = []
            networks = []
            for network in status_items[:32]:
                if not isinstance(network, dict):
                    continue
                networks.append({
                    "name": network.get("name"),
                    "interface": network.get("interface"),
                    "ips": network.get("ips", [])[:16]
                    if isinstance(network.get("ips", []), list) else [],
                    "default": network.get("default") is True,
                })
            attached_names = {
                item["name"] for item in networks if isinstance(item.get("name"), str)
            }
            default_networks = [
                item for item in networks if item.get("default") is True
            ]
            if require_data_plane_default:
                expected_name = expected_default_network
                default_network_valid = (
                    len(default_networks) == 1
                    and isinstance(default_networks[0].get("name"), str)
                    and (
                        default_networks[0]["name"] == expected_name
                        or (
                            "/" not in expected_name
                            and default_networks[0]["name"].rsplit("/", 1)[-1]
                            == expected_name
                        )
                    )
                )
            else:
                default_network_valid = None
            missing = []
            for expected in expected_attachments:
                if "/" in expected:
                    attached = expected in attached_names
                else:
                    attached = any(
                        item == expected or item.rsplit("/", 1)[-1] == expected
                        for item in attached_names
                    )
                if not attached:
                    missing.append(expected)
            pod_results.append({
                "name": metadata.get("name"),
                "phase": pod.get("status", {}).get("phase"),
                "node_name": pod.get("spec", {}).get("nodeName"),
                "networks": networks,
                "default_networks": default_networks,
                "expected_default_network": expected_default_network,
                "default_network_valid": default_network_valid,
                "missing_expected_attachments": missing,
                "attachments_valid": not missing,
            })
        workload_results.append({
            "namespace": namespace,
            "inspection_status": "observed",
            "pod_count": len(pod_results),
            "inspected_pod_count": min(len(pod_results), 500),
            "inspection_truncated": len(pod_results) > 500,
            "pods_missing_expected_attachments": [
                item["name"] for item in pod_results[:500] if not item["attachments_valid"]
            ],
            "pods": pod_results[:500],
        })
    workload_inspection_complete = (
        len(workload_results) == len(workload_namespaces)
        and all(item.get("inspection_status") == "observed" for item in workload_results)
        and (
            not expected_attachments
            or all(item.get("pod_count", 0) > 0 for item in workload_results)
        )
    )
    workload_attachments_valid = (
        not missing_network_definitions
        and all(
            not item.get("pods_missing_expected_attachments")
            for item in workload_results
            if item.get("inspection_status") == "observed"
        )
    )
    workload_default_network_complete = (
        require_data_plane_default
        and workload_inspection_complete
        and all(
            item.get("inspection_status") == "observed"
            and item.get("pod_count", 0) > 0
            and all(
                pod.get("default_network_valid") is True
                for pod in item.get("pods", [])
            )
            for item in workload_results
        )
    )
    workload_default_network_passed = (
        workload_default_network_complete
        and all(
            item.get("inspection_status") == "observed"
            and not item.get("inspection_truncated")
            and all(
                pod.get("default_network_valid") is True
                for pod in item.get("pods", [])
            )
            for item in workload_results
        )
    )
    agent_pod_network = _pod_network_namespace_snapshot()
    agent_default_route_requested = expected_agent_default_interface is not None
    agent_default_route_passed = (
        any(
            route.get("interface") == expected_agent_default_interface
            and (
                expected_agent_default_gateway is None
                or route.get("gateway") == expected_agent_default_gateway
            )
            for route in agent_pod_network.get("default_routes", [])
        )
        if agent_default_route_requested
        else None
    )
    return {
        "inspection_status": "observed",
        "kubernetes_version": version.get("gitVersion"),
        "node_count": len(nodes),
        "observed_components": observed_components,
        "network_attachment_definition_count": len(network_attachments),
        "network_attachments": network_attachments[:500],
        "workload_namespace_results": workload_results,
        "workload_attachment_validation": {
            "requested": bool(workload_namespaces or expected_attachments),
            "complete": workload_inspection_complete,
            "passed": (
                workload_attachments_valid
                if expected_attachments and workload_inspection_complete
                else None
            ),
        },
        "agent_pod_default_route_validation": {
            "requested": agent_default_route_requested,
            "expected_interface": expected_agent_default_interface,
            "expected_gateway": expected_agent_default_gateway,
            "passed": agent_default_route_passed,
            "scope": agent_pod_network.get("scope"),
        },
        "pod_default_network_validation": {
            "requested": require_data_plane_default,
            "expected_default_network": expected_default_network,
            "complete": (
                workload_inspection_complete
                and all(
                    item.get("inspection_status") == "observed"
                    and not item.get("inspection_truncated")
                    and item.get("pod_count", 0) > 0
                    for item in workload_results
                )
                if require_data_plane_default
                else False
            ),
            "passed": (
                workload_default_network_passed
                if require_data_plane_default
                else None
            ),
        },
        "observed_attachment_plugin_types": plugin_types,
        "missing_expected_network_attachment_definitions": missing_network_definitions,
        "node_network_resources": node_network_resources[:500],
        "host_network_hardware": inspect_host_network_hardware(),
        "physical_node_hardware_inventory": (
            inspect_cluster_node_hardware_inventory(nodes)
            if node_type == "target_cluster"
            else {"status": "not_applicable", "nodes": []}
        ),
        "agent_pod_network": agent_pod_network,
        "limitations": [
            "Pod namespace interfaces are not host NIC inventory.",
            "A NetworkAttachmentDefinition does not prove a VF is allocated or a Pod is attached.",
            (
                "DPDK readiness requires host IOMMU/VFIO, hugepage, CPU/NUMA "
                "and device binding validation."
            ),
            "Component presence is inferred from Pod names, not dataplane connectivity tests.",
        ],
        "side_effects": False,
    }


def execute_command(command: dict[str, Any], node_type: str) -> dict[str, Any]:
    command_type = command.get("command_type")
    payload = command.get("payload", {})
    if not isinstance(payload, dict):
        raise NodeAgentError("INVALID_PAYLOAD")
    if contains_forbidden_fields(payload):
        raise NodeAgentError("FORBIDDEN_PAYLOAD_FIELD")
    allowed = {
        "eph_node": {
            "node.health",
            "host.preflight",
            "cluster.plan",
            "cluster.health",
            "cluster.network.inspect",
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "k8s.resource.read",
            "capi.cluster.observe",
            "metal3.host.inspect",
            "cluster.provider.discover",
            "cluster.manifest.validate",
            "cluster.capi.pivot.source.preflight",
            "cluster.capi.pivot.dry-run",
            "cluster.capi.pivot.execute",
        },
        "target_cluster": {
            "node.health",
            "cluster.health",
            "cluster.network.inspect",
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "cluster.control-plane.verify",
            "cluster.capi.pivot.target.preflight",
            "cluster.capi.pivot.target.verify",
            "cluster.capi.worker.preflight",
            "k8s.resource.read",
        },
    }
    if command_type not in allowed.get(node_type, set()):
        raise NodeAgentError("CAPABILITY_NOT_CONFIGURED")
    if command_type == "node.health":
        return {
            "node_type": node_type,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "agent_status": "ready",
        }
    if command_type == "cluster.health":
        version = kubernetes_api_get("/version")
        return {"api_reachable": True, "kubernetes_version": version.get("gitVersion")}
    if command_type == "cluster.network.inspect":
        return inspect_network_environment(payload, node_type)
    if command_type == "cluster.observe":
        version = kubernetes_api_get("/version")
        nodes = kubernetes_api_get("/api/v1/nodes")
        api_groups = kubernetes_api_get("/apis")
        return {
            "kubernetes_version": version.get("gitVersion"),
            "node_count": len(nodes.get("items", [])),
            "node_names": [
                item.get("metadata", {}).get("name")
                for item in nodes.get("items", [])
            ],
            "api_group_count": len(api_groups.get("groups", [])),
        }
    if command_type == "cluster.control-plane.verify":
        expected_replicas = payload.get("expected_control_plane_replicas", 1)
        if (
            not isinstance(expected_replicas, int)
            or isinstance(expected_replicas, bool)
            or expected_replicas < 1
            or expected_replicas > 9
            or expected_replicas % 2 == 0
        ):
            raise NodeAgentError("INVALID_CONTROL_PLANE_REPLICAS")
        version = kubernetes_api_get("/version")
        nodes = kubernetes_api_get("/api/v1/nodes").get("items", [])
        control_plane_nodes = []
        not_ready_nodes = []
        for item in nodes:
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            is_control_plane = (
                "node-role.kubernetes.io/control-plane" in labels
                or "node-role.kubernetes.io/master" in labels
            )
            if not is_control_plane:
                continue
            conditions = item.get("status", {}).get("conditions", [])
            ready = next(
                (condition for condition in conditions if condition.get("type") == "Ready"),
                {},
            )
            name = metadata.get("name")
            control_plane_nodes.append(name)
            if ready.get("status") != "True":
                not_ready_nodes.append(name)
        ready = (
            len(control_plane_nodes) >= expected_replicas
            and not not_ready_nodes
        )
        return {
            "control_plane_ready": ready,
            "kubernetes_version": version.get("gitVersion"),
            "expected_control_plane_replicas": expected_replicas,
            "ready_control_plane_replicas": len(control_plane_nodes) - len(not_ready_nodes),
            "control_plane_nodes": control_plane_nodes[:9],
            "not_ready_control_plane_nodes": not_ready_nodes[:9],
            "worker_provisioning_allowed": ready,
            "capi_pivot_allowed": ready,
            "side_effects": False,
        }
    if command_type == "cluster.capi.pivot.source.preflight":
        cluster_name = payload.get("cluster_name")
        if (
            not isinstance(cluster_name, str)
            or len(cluster_name) > 63
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
        ):
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        resource_inventory = {
            "cluster.x-k8s.io": [
                "clusters",
                "machines",
                "machinesets",
                "machinedeployments",
            ],
            "bootstrap.cluster.x-k8s.io": [
                "kubeadmconfigs",
                "kubeadmconfigtemplates",
            ],
            "controlplane.cluster.x-k8s.io": ["kubeadmcontrolplanes"],
            "infrastructure.cluster.x-k8s.io": [
                "metal3clusters",
                "metal3machines",
                "metal3machinetemplates",
            ],
            "metal3.io": ["baremetalhosts"],
        }
        discovery = kubernetes_api_get("/apis")
        discovered_groups = {
            item.get("name"): item
            for item in discovery.get("groups", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        missing_resources = []
        provider_versions = {}
        full_inventory = []
        resource_counts = {}
        pagination_required = []
        for group_name, resource_names in resource_inventory.items():
            group = discovered_groups.get(group_name)
            preferred = group.get("preferredVersion", {}) if group else {}
            version = preferred.get("version")
            if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.]+", version):
                missing_resources.extend(
                    f"{group_name}/{resource_name}" for resource_name in resource_names
                )
                continue
            provider_versions[group_name] = version
            version_document = kubernetes_api_get(f"/apis/{group_name}/{version}")
            api_resources = {
                item.get("name"): item
                for item in version_document.get("resources", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            for resource_name in resource_names:
                if api_resources.get(resource_name) is None:
                    missing_resources.append(f"{group_name}/{resource_name}")
                    continue
                path = f"/apis/{group_name}/{version}/{resource_name}"
                items = list_all_kubernetes_items(path)
                resource_key = f"{group_name}/{resource_name}"
                resource_counts[resource_key] = len(items)
                for item in items:
                    metadata = item.get("metadata", {})
                    owner_references = metadata.get("ownerReferences", [])
                    labels = metadata.get("labels", {})
                    if not isinstance(owner_references, list) or not isinstance(labels, dict):
                        raise NodeAgentError("CAPI_SOURCE_INVENTORY_INVALID")
                    full_inventory.append(
                        {
                            "api_version": item.get("apiVersion"),
                            "kind": item.get("kind"),
                            "resource": resource_key,
                            "name": metadata.get("name"),
                            "namespace": metadata.get("namespace"),
                            "uid": metadata.get("uid"),
                            "owner_references": [
                                {
                                    "api_version": owner.get("apiVersion"),
                                    "kind": owner.get("kind"),
                                    "name": owner.get("name"),
                                    "uid": owner.get("uid"),
                                    "controller": owner.get("controller", False),
                                }
                                for owner in owner_references[:10]
                                if isinstance(owner, dict)
                            ],
                            "owner_references_truncated": len(owner_references) > 10,
                            "cluster_name": labels.get("cluster.x-k8s.io/cluster-name"),
                        }
                    )
        expected_controllers = {
            "capi-system": "capi-controller-manager",
            "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
            "capi-kubeadm-control-plane-system": (
                "capi-kubeadm-control-plane-controller-manager"
            ),
            "capm3-system": "capm3-controller-manager",
        }
        controller_states = []
        for controller_namespace, deployment_name in expected_controllers.items():
            try:
                deployments = kubernetes_api_get(
                    f"/apis/apps/v1/namespaces/{controller_namespace}/deployments"
                )
            except NodeAgentError as exc:
                if str(exc) != "KUBERNETES_API_HTTP_404":
                    raise
                deployments = {"items": []}
            deployment = next(
                (
                    item
                    for item in deployments.get("items", [])
                    if item.get("metadata", {}).get("name") == deployment_name
                ),
                {},
            )
            metadata = deployment.get("metadata", {})
            status_data = deployment.get("status", {})
            desired = deployment.get("spec", {}).get("replicas", 1)
            available = status_data.get("availableReplicas")
            generation = metadata.get("generation", 1)
            observed = status_data.get("observedGeneration", 0)
            controller_states.append(
                {
                    "namespace": controller_namespace,
                    "deployment": deployment_name,
                    "ready": (
                        isinstance(desired, int)
                        and not isinstance(desired, bool)
                        and desired > 0
                        and isinstance(available, int)
                        and not isinstance(available, bool)
                        and available >= desired
                        and isinstance(generation, int)
                        and not isinstance(generation, bool)
                        and isinstance(observed, int)
                        and not isinstance(observed, bool)
                        and observed >= generation
                    ),
                }
            )
        controllers_ready = all(item["ready"] for item in controller_states)
        inventory_truncated = False
        inventory_complete = not missing_resources
        source_cluster_matches = [
            item
            for item in full_inventory
            if item.get("resource") == "cluster.x-k8s.io/clusters"
            and item.get("name") == cluster_name
        ]
        source_cluster_identity = None
        if len(source_cluster_matches) == 1:
            source_cluster = source_cluster_matches[0]
            if (
                isinstance(source_cluster.get("uid"), str)
                and source_cluster["uid"]
                and isinstance(source_cluster.get("namespace"), str)
                and source_cluster["namespace"]
                and isinstance(source_cluster.get("api_version"), str)
                and source_cluster["api_version"]
            ):
                source_cluster_identity = {
                    "name": cluster_name,
                    "namespace": source_cluster["namespace"],
                    "uid": source_cluster["uid"],
                    "api_version": source_cluster.get("api_version"),
                }
        owner_graph = build_capi_owner_graph(
            full_inventory,
            cluster_name,
            source_cluster_identity.get("uid") if source_cluster_identity else "",
            inventory_complete,
        )
        summary_candidates = list(full_inventory)
        if source_cluster_identity:
            root_item = source_cluster_matches[0]
            summary_candidates = [
                root_item,
                *[
                    item
                    for item in full_inventory
                    if item.get("uid") != source_cluster_identity["uid"]
                ],
            ]
        inventory = []
        for item in summary_candidates:
            if len(inventory) >= 200:
                break
            candidate = [*inventory, item]
            encoded_candidate = json.dumps(
                candidate,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_candidate) > 48 * 1024:
                break
            inventory.append(item)
        inventory_summary_truncated = len(inventory) < len(full_inventory)
        blockers = []
        if missing_resources:
            blockers.append("required_source_capi_metal3_apis_missing")
        if not controllers_ready:
            blockers.append("source_management_controllers_not_ready")
        if inventory_truncated:
            blockers.append("source_inventory_incomplete")
        if not inventory:
            blockers.append("source_inventory_empty")
        if not owner_graph["complete"]:
            blockers.append("source_owner_graph_incomplete")
        if not source_cluster_matches:
            blockers.append("requested_source_cluster_not_found")
        elif len(source_cluster_matches) > 1:
            blockers.append("requested_source_cluster_identity_ambiguous")
        elif source_cluster_identity is None:
            blockers.append("requested_source_cluster_identity_invalid")
        return {
            "source_management_plane_ready": (
                inventory_complete
                and controllers_ready
                and bool(inventory)
                and source_cluster_identity is not None
                and len(source_cluster_matches) == 1
                and owner_graph["complete"]
            ),
            "source_cluster_identity": source_cluster_identity,
            "owner_graph": owner_graph,
            "cluster_name": cluster_name,
            "provider_versions": provider_versions,
            "resource_counts": resource_counts,
            "inventory": inventory[:200],
            "inventory_truncated": inventory_truncated,
            "inventory_summary_truncated": inventory_summary_truncated,
            "inventory_total_count": len(full_inventory),
            "missing_resources": missing_resources[:100],
            "pagination_required": sorted(set(pagination_required))[:100],
            "controllers": controller_states,
            "blockers": blockers,
            "side_effects": False,
        }
    if command_type == "cluster.capi.pivot.target.verify":
        cluster_name = payload.get("cluster_name")
        namespace = payload.get("namespace", "default")
        expected_uid = payload.get("expected_cluster_uid")
        if (
            not isinstance(cluster_name, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
            or len(cluster_name) > 63
        ):
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        if (
            not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
            or len(namespace) > 63
        ):
            raise NodeAgentError("INVALID_NAMESPACE")
        if not isinstance(expected_uid, str) or not expected_uid:
            raise NodeAgentError("EXPECTED_SOURCE_CLUSTER_UID_REQUIRED")
        discovery = kubernetes_api_get("/apis")
        cluster_group = next(
            (
                item for item in discovery.get("groups", [])
                if item.get("name") == "cluster.x-k8s.io"
            ),
            None,
        )
        version = (
            cluster_group.get("preferredVersion", {}).get("version")
            if isinstance(cluster_group, dict)
            else None
        )
        if not isinstance(version, str):
            raise NodeAgentError("TARGET_CAPI_API_NOT_DISCOVERED")
        cluster = kubernetes_api_get(
            f"/apis/cluster.x-k8s.io/{version}"
            f"/namespaces/{namespace}/clusters/{cluster_name}"
        )
        metadata = cluster.get("metadata", {})
        actual_uid = metadata.get("uid")
        if actual_uid != expected_uid:
            raise NodeAgentError("TARGET_CLUSTER_UID_MISMATCH")
        machines = kubernetes_api_get(
            f"/apis/cluster.x-k8s.io/{version}"
            f"/namespaces/{namespace}/machines"
        )
        machine_items = machines.get("items", [])
        if not isinstance(machine_items, list):
            raise NodeAgentError("INVALID_TARGET_MACHINE_INVENTORY")
        cluster_machines = [
            item
            for item in machine_items
            if isinstance(item, dict)
            and item.get("spec", {}).get("clusterName") == cluster_name
        ]
        machine_names = [
            item.get("metadata", {}).get("name")
            for item in cluster_machines
            if isinstance(item.get("metadata", {}).get("name"), str)
            and item.get("metadata", {}).get("name")
        ]
        if (
            len(machine_names) != len(cluster_machines)
            or len(set(machine_names)) != len(machine_names)
        ):
            raise NodeAgentError("INVALID_TARGET_MACHINE_IDENTITY")
        ready_machines = [
            item.get("metadata", {}).get("name")
            for item in cluster_machines
            if any(
                isinstance(condition, dict)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", [])
            )
        ]
        not_ready_machines = sorted(set(machine_names) - set(ready_machines))
        expected_controllers = {
            "capi-system": "capi-controller-manager",
            "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
            "capi-kubeadm-control-plane-system": "capi-kubeadm-control-plane-controller-manager",
            "capm3-system": "capm3-controller-manager",
        }
        controller_states = []
        for controller_namespace, deployment_name in expected_controllers.items():
            deployment = kubernetes_api_get(
                f"/apis/apps/v1/namespaces/{controller_namespace}"
                f"/deployments/{deployment_name}"
            )
            desired = deployment.get("spec", {}).get("replicas", 1)
            available = deployment.get("status", {}).get("availableReplicas", 0)
            observed = deployment.get("status", {}).get("observedGeneration", 0)
            generation = deployment.get("metadata", {}).get("generation", 1)
            ready = (
                isinstance(desired, int)
                and not isinstance(desired, bool)
                and desired > 0
                and isinstance(available, int)
                and not isinstance(available, bool)
                and available >= desired
                and isinstance(observed, int)
                and isinstance(generation, int)
                and observed >= generation
            )
            controller_states.append(
                {
                    "namespace": controller_namespace,
                    "deployment": deployment_name,
                    "ready": ready,
                }
            )
        controllers_ready = all(item["ready"] for item in controller_states)
        machine_inventory_ready = bool(machine_names) and len(ready_machines) == len(machine_names)
        adoption_verified = controllers_ready and machine_inventory_ready
        return {
            "cluster_name": cluster_name,
            "namespace": namespace,
            "cluster_uid": actual_uid,
            "source_cluster_uid": expected_uid,
            "cluster_identity_preserved": True,
            "machine_count": len(machine_names),
            "machine_names": machine_names[:200],
            "ready_machine_count": len(ready_machines),
            "ready_machine_names": ready_machines[:200],
            "not_ready_machine_names": not_ready_machines[:200],
            "machine_inventory_ready": machine_inventory_ready,
            "controllers": controller_states,
            "controllers_ready": controllers_ready,
            "pivot_adoption_verified": adoption_verified,
            "side_effects": False,
        }
    if command_type == "cluster.capi.pivot.target.preflight":
        cluster_name = payload.get("cluster_name")
        namespace = payload.get("namespace", "default")
        if (
            not isinstance(cluster_name, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
            or len(cluster_name) > 63
        ):
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        if (
            not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
            or len(namespace) > 63
        ):
            raise NodeAgentError("INVALID_NAMESPACE")
        required_resources = {
            "cluster.x-k8s.io": {
                "clusters",
                "machines",
                "machinesets",
                "machinedeployments",
            },
            "bootstrap.cluster.x-k8s.io": {
                "kubeadmconfigs",
                "kubeadmconfigtemplates",
            },
            "controlplane.cluster.x-k8s.io": {"kubeadmcontrolplanes"},
            "infrastructure.cluster.x-k8s.io": {
                "metal3clusters",
                "metal3machines",
                "metal3machinetemplates",
            },
            "metal3.io": {"baremetalhosts"},
        }
        discovery = kubernetes_api_get("/apis")
        discovered_groups = {
            item.get("name"): item
            for item in discovery.get("groups", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        missing_resources = []
        group_versions = {}
        for group_name, expected_resources in required_resources.items():
            group = discovered_groups.get(group_name)
            preferred = group.get("preferredVersion", {}) if group else {}
            version = preferred.get("version")
            if not isinstance(version, str):
                missing_resources.extend(
                    f"{group_name}/{resource}" for resource in sorted(expected_resources)
                )
                continue
            group_versions[group_name] = version
            version_data = kubernetes_api_get(f"/apis/{group_name}/{version}")
            available = {
                item.get("name")
                for item in version_data.get("resources", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            missing_resources.extend(
                f"{group_name}/{resource}"
                for resource in sorted(expected_resources - available)
            )
        expected_controllers = {
            "capi-system": "capi-controller-manager",
            "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
            "capi-kubeadm-control-plane-system": (
                "capi-kubeadm-control-plane-controller-manager"
            ),
            "capm3-system": "capm3-controller-manager",
        }
        controller_states = []
        for controller_namespace, deployment_name in expected_controllers.items():
            try:
                deployments = kubernetes_api_get(
                    f"/apis/apps/v1/namespaces/{controller_namespace}/deployments"
                )
            except NodeAgentError as exc:
                if str(exc) != "KUBERNETES_API_HTTP_404":
                    raise
                deployments = {"items": []}
            deployment = next(
                (
                    item
                    for item in deployments.get("items", [])
                    if item.get("metadata", {}).get("name") == deployment_name
                ),
                {},
            )
            metadata = deployment.get("metadata", {})
            status_data = deployment.get("status", {})
            available_replicas = status_data.get("availableReplicas")
            desired_replicas = deployment.get("spec", {}).get("replicas", 1)
            observed_generation = status_data.get("observedGeneration", 0)
            generation = metadata.get("generation", 1)
            ready = (
                isinstance(available_replicas, int)
                and not isinstance(available_replicas, bool)
                and isinstance(desired_replicas, int)
                and not isinstance(desired_replicas, bool)
                and desired_replicas > 0
                and available_replicas >= desired_replicas
                and isinstance(observed_generation, int)
                and not isinstance(observed_generation, bool)
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and observed_generation >= generation
            )
            controller_states.append(
                {
                    "namespace": controller_namespace,
                    "deployment": deployment_name,
                    "ready": ready,
                }
            )
        cluster_conflicts = []
        cluster_api_version = group_versions.get("cluster.x-k8s.io")
        if cluster_api_version:
            try:
                clusters = kubernetes_api_get(
                    f"/apis/cluster.x-k8s.io/{cluster_api_version}"
                    f"/namespaces/{namespace}/clusters"
                )
            except NodeAgentError as exc:
                if str(exc) != "KUBERNETES_API_HTTP_404":
                    raise
                clusters = {"items": []}
            cluster_conflicts = [
                item.get("metadata", {}).get("name")
                for item in clusters.get("items", [])
                if item.get("metadata", {}).get("name") == cluster_name
            ]
        controllers_ready = all(item["ready"] for item in controller_states)
        target_ready = (
            not missing_resources
            and controllers_ready
            and not cluster_conflicts
        )
        blockers = []
        if missing_resources:
            blockers.append("required_capi_metal3_apis_missing")
        if not controllers_ready:
            blockers.append("management_controllers_not_ready")
        if cluster_conflicts:
            blockers.append("target_cluster_name_conflict")
        return {
            "target_management_plane_ready": target_ready,
            "pivot_execution_allowed": target_ready,
            "cluster_name": cluster_name,
            "namespace": namespace,
            "missing_resources": missing_resources[:100],
            "provider_versions": group_versions,
            "controllers": controller_states,
            "cluster_name_conflicts": cluster_conflicts[:20],
            "blockers": blockers,
            "side_effects": False,
        }
    if command_type == "cluster.capi.worker.preflight":
        cluster_name = payload.get("cluster_name")
        namespace = payload.get("namespace", "default")
        expected_replicas = payload.get("expected_worker_replicas")
        config = payload.get("worker_provisioning")
        if (
            not isinstance(cluster_name, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
            or len(cluster_name) > 63
        ):
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        if (
            not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
            or len(namespace) > 63
        ):
            raise NodeAgentError("INVALID_NAMESPACE")
        if (
            not isinstance(expected_replicas, int)
            or isinstance(expected_replicas, bool)
            or not 1 <= expected_replicas <= 1000
        ):
            raise NodeAgentError("INVALID_WORKER_REPLICAS")
        if not isinstance(config, dict):
            raise NodeAgentError("WORKER_PROVISIONING_CONFIG_REQUIRED")
        deployment_name = config.get("machine_deployment_name")
        infrastructure_template_name = config.get("infrastructure_template_name")
        bootstrap_template_name = config.get("bootstrap_template_name")
        host_namespace = config.get("baremetal_host_namespace")
        host_selector = config.get("host_selector")
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value)
            or len(value) > 63
            for value in (
                deployment_name,
                infrastructure_template_name,
                bootstrap_template_name,
                host_namespace,
            )
        ):
            raise NodeAgentError("INVALID_WORKER_PROVISIONING_REFERENCE")
        if (
            not isinstance(host_selector, dict)
            or not host_selector
            or len(host_selector) > 32
            or any(
                not isinstance(key, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?"
                    r"(/[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?)?",
                    key,
                )
                or not isinstance(value, str)
                or not value
                or len(value) > 128
                for key, value in host_selector.items()
            )
        ):
            raise NodeAgentError("INVALID_BAREMETAL_HOST_SELECTOR")
        network_profile = validate_network_profile(config.get("network_profile"))
        discovery = kubernetes_api_get("/apis")
        groups = {
            item.get("name"): item
            for item in discovery.get("groups", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        required = {
            "cluster.x-k8s.io": {"machinedeployments"},
            "bootstrap.cluster.x-k8s.io": {"kubeadmconfigtemplates"},
            "infrastructure.cluster.x-k8s.io": {"metal3machines", "metal3machinetemplates"},
            "metal3.io": {"baremetalhosts"},
        }
        versions = {}
        missing_resources = []
        for group_name, expected in required.items():
            group = groups.get(group_name)
            version = group.get("preferredVersion", {}).get("version") if group else None
            if not isinstance(version, str):
                missing_resources.extend(
                    f"{group_name}/{resource}" for resource in sorted(expected)
                )
                continue
            versions[group_name] = version
            api = kubernetes_api_get(f"/apis/{group_name}/{version}")
            available = {
                item.get("name")
                for item in api.get("resources", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            missing_resources.extend(
                f"{group_name}/{resource}"
                for resource in sorted(expected - available)
            )
        if missing_resources:
            return {
                "ready": False,
                "cluster_name": cluster_name,
                "namespace": namespace,
                "expected_worker_replicas": expected_replicas,
                "missing_resources": missing_resources,
                "blockers": ["required_worker_provisioning_apis_missing"],
                "side_effects": False,
                "worker_creation_performed": False,
            }
        expected_controllers = {
            "capi-system": "capi-controller-manager",
            "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
            "capi-kubeadm-control-plane-system": (
                "capi-kubeadm-control-plane-controller-manager"
            ),
            "capm3-system": "capm3-controller-manager",
        }
        controller_states = []
        for controller_namespace, controller_name in expected_controllers.items():
            try:
                controller = kubernetes_api_get(
                    f"/apis/apps/v1/namespaces/{controller_namespace}"
                    f"/deployments/{controller_name}"
                )
            except NodeAgentError as exc:
                if str(exc) != "KUBERNETES_API_HTTP_404":
                    raise
                controller = {}
            metadata = controller.get("metadata", {})
            spec = controller.get("spec", {})
            status = controller.get("status", {})
            desired = spec.get("replicas", 1)
            available = status.get("availableReplicas", 0)
            generation = metadata.get("generation", 1)
            observed = status.get("observedGeneration", 0)
            controller_ready = (
                isinstance(desired, int)
                and not isinstance(desired, bool)
                and desired > 0
                and isinstance(available, int)
                and not isinstance(available, bool)
                and available >= desired
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and isinstance(observed, int)
                and not isinstance(observed, bool)
                and observed >= generation
            )
            controller_states.append(
                {
                    "namespace": controller_namespace,
                    "deployment": controller_name,
                    "ready": controller_ready,
                }
            )
        cluster_version = versions["cluster.x-k8s.io"]
        cluster_path = (
            f"/apis/cluster.x-k8s.io/{cluster_version}/namespaces/{namespace}/clusters/{cluster_name}"
        )
        cluster = kubernetes_api_get(cluster_path)
        cluster_uid = cluster.get("metadata", {}).get("uid")
        if not isinstance(cluster_uid, str) or not cluster_uid:
            raise NodeAgentError("TARGET_CLUSTER_UID_MISSING")
        md_path = (
            f"/apis/cluster.x-k8s.io/{cluster_version}/namespaces/{namespace}/machinedeployments/{deployment_name}"
        )
        existing_md = None
        try:
            existing_md = kubernetes_api_get(md_path)
        except NodeAgentError as exc:
            if str(exc) != "KUBERNETES_API_HTTP_404":
                raise
        infra_version = versions["infrastructure.cluster.x-k8s.io"]
        infra_path = (
            f"/apis/infrastructure.cluster.x-k8s.io/{infra_version}/namespaces/{namespace}/metal3machinetemplates/{infrastructure_template_name}"
        )
        bootstrap_version = versions["bootstrap.cluster.x-k8s.io"]
        bootstrap_path = (
            f"/apis/bootstrap.cluster.x-k8s.io/{bootstrap_version}/namespaces/{namespace}/kubeadmconfigtemplates/{bootstrap_template_name}"
        )
        infrastructure_template = kubernetes_api_get(infra_path)
        bootstrap_template = kubernetes_api_get(bootstrap_path)
        bmh_version = versions["metal3.io"]
        bmh_items = list_all_kubernetes_items(
            f"/apis/metal3.io/{bmh_version}/namespaces/{host_namespace}/baremetalhosts",
            max_items=10000,
        )
        selected_hosts = []
        unavailable_hosts = []
        invalid_host_identities = []
        seen_host_names = set()
        seen_host_uids = set()
        for host in bmh_items:
            metadata = host.get("metadata", {})
            labels = metadata.get("labels", {})
            if not isinstance(labels, dict) or any(
                labels.get(key) != value for key, value in host_selector.items()
            ):
                continue
            host_name = metadata.get("name")
            host_uid = metadata.get("uid")
            if (
                not isinstance(host_name, str)
                or not host_name
                or not isinstance(host_uid, str)
                or not host_uid
                or host_name in seen_host_names
                or host_uid in seen_host_uids
            ):
                invalid_host_identities.append(
                    {
                        "name": host_name if isinstance(host_name, str) else None,
                        "uid": host_uid if isinstance(host_uid, str) else None,
                    }
                )
                continue
            seen_host_names.add(host_name)
            seen_host_uids.add(host_uid)
            status = host.get("status", {})
            status = status if isinstance(status, dict) else {}
            provisioning = status.get("provisioning", {})
            provisioning = provisioning if isinstance(provisioning, dict) else {}
            spec = host.get("spec", {})
            spec = spec if isinstance(spec, dict) else {}
            state = provisioning.get("state")
            consumer = spec.get("consumerRef")
            operational_status = status.get("operationalStatus")
            error_message = status.get("errorMessage")
            host_error = bool(status.get("error")) or (
                isinstance(error_message, str) and bool(error_message.strip())
            )
            host_ready = (
                spec.get("online") is True
                and state == "available"
                and not consumer
                and not host_error
                and operational_status in (None, "OK")
            )
            entry = {
                "name": host_name,
                "uid": host_uid,
                "provisioning_state": state,
                "operational_status": operational_status,
                "online": spec.get("online") is True,
                "has_consumer": bool(consumer),
                "has_error": host_error,
                "ready": host_ready,
            }
            if host_ready:
                selected_hosts.append(entry)
            else:
                unavailable_hosts.append(entry)
        blockers = []
        if not all(item["ready"] for item in controller_states):
            blockers.append("required_capi_provider_controllers_not_ready")
        if invalid_host_identities:
            blockers.append("baremetal_host_identity_invalid_or_duplicate")
        if existing_md is not None:
            blockers.append("machine_deployment_name_already_exists")
        if len(selected_hosts) < expected_replicas:
            blockers.append("insufficient_available_baremetal_hosts")
        infrastructure_metadata = infrastructure_template.get("metadata", {})
        bootstrap_metadata = bootstrap_template.get("metadata", {})
        if (
            not isinstance(infrastructure_metadata.get("uid"), str)
            or not infrastructure_metadata.get("uid")
            or not isinstance(bootstrap_metadata.get("uid"), str)
            or not bootstrap_metadata.get("uid")
            or infrastructure_metadata.get("name") != infrastructure_template_name
            or bootstrap_metadata.get("name") != bootstrap_template_name
            or infrastructure_metadata.get("namespace") != namespace
            or bootstrap_metadata.get("namespace") != namespace
        ):
            blockers.append("worker_template_identity_or_namespace_invalid")
        if infrastructure_template.get("apiVersion") != (
            "infrastructure.cluster.x-k8s.io/" + infra_version
        ):
            blockers.append("metal3_machine_template_api_version_mismatch")
        if bootstrap_template.get("apiVersion") != (
            "bootstrap.cluster.x-k8s.io/" + bootstrap_version
        ):
            blockers.append("kubeadm_config_template_api_version_mismatch")
        if any(
            item.get("has_error") is True
            or item.get("operational_status") not in (None, "OK")
            for item in unavailable_hosts
        ):
            blockers.append("matching_baremetal_host_operational_error")
        ready = not blockers
        return {
            "ready": ready,
            "cluster_name": cluster_name,
            "cluster_uid": cluster_uid,
            "namespace": namespace,
            "expected_worker_replicas": expected_replicas,
            "machine_deployment_name": deployment_name,
            "machine_deployment_exists": existing_md is not None,
            "infrastructure_template": {
                "name": infrastructure_template_name,
                "uid": infrastructure_template.get("metadata", {}).get("uid"),
                "api_version": infrastructure_template.get("apiVersion"),
            },
            "bootstrap_template": {
                "name": bootstrap_template_name,
                "uid": bootstrap_template.get("metadata", {}).get("uid"),
                "api_version": bootstrap_template.get("apiVersion"),
            },
            "baremetal_host_namespace": host_namespace,
            "host_selector": host_selector,
            "available_baremetal_host_count": len(selected_hosts),
            "selected_baremetal_hosts": selected_hosts[:100],
            "unavailable_matching_hosts": unavailable_hosts[:100],
            "network_profile": network_profile,
            "provider_versions": versions,
            "controllers": controller_states,
            "controllers_ready": all(item["ready"] for item in controller_states),
            "blockers": blockers,
            "side_effects": False,
            "worker_creation_performed": False,
            "execution_allowed": False,
        }
    if command_type == "cluster.postflight":
        nodes = kubernetes_api_get("/api/v1/nodes")
        items = nodes.get("items", [])
        not_ready = []
        node_ready_by_name = {}
        for item in items:
            metadata = item.get("metadata", {})
            name = metadata.get("name")
            conditions = item.get("status", {}).get("conditions", [])
            ready = next(
                (
                    condition
                    for condition in conditions
                    if condition.get("type") == "Ready"
                ),
                {},
            )
            is_ready = (
                isinstance(name, str)
                and bool(name)
                and ready.get("status") == "True"
            )
            if isinstance(name, str) and name:
                node_ready_by_name[name] = is_ready
            if not is_ready:
                not_ready.append(name)

        result = {
            "node_count": len(items),
            "ready": len(items) > 0 and not not_ready,
            "not_ready_nodes": not_ready,
        }
        expected_workers = payload.get("expected_worker_replicas")
        if expected_workers is None:
            return result
        namespace = payload.get("namespace")
        cluster_name = payload.get("cluster_name")
        if (
            not isinstance(expected_workers, int)
            or isinstance(expected_workers, bool)
            or expected_workers < 0
            or expected_workers > 10000
            or not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace)
            or not isinstance(cluster_name, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
        ):
            raise NodeAgentError("INVALID_CAPI_WORKER_POSTFLIGHT_SPEC")
        api_groups = kubernetes_api_get("/apis").get("groups", [])
        capi_group = next(
            (
                group
                for group in api_groups
                if isinstance(group, dict) and group.get("name") == "cluster.x-k8s.io"
            ),
            {},
        )
        machine_version = capi_group.get("preferredVersion", {}).get("version")
        if not isinstance(machine_version, str) or not re.fullmatch(
            r"v[0-9]+(alpha[0-9]+|beta[0-9]+)?", machine_version
        ):
            raise NodeAgentError("CAPI_MACHINE_API_UNAVAILABLE")
        machine_path = (
            f"/apis/cluster.x-k8s.io/{machine_version}/namespaces/"
            f"{namespace}/machines"
        )
        machines = list_all_kubernetes_items(machine_path)
        worker_machines = []
        machine_names = set()
        for machine in machines:
            metadata = machine.get("metadata", {})
            spec = machine.get("spec", {})
            labels = metadata.get("labels", {})
            if spec.get("clusterName") != cluster_name:
                continue
            if "cluster.x-k8s.io/control-plane" in labels:
                continue
            machine_name = metadata.get("name")
            if not isinstance(machine_name, str) or not machine_name:
                raise NodeAgentError("CAPI_WORKER_MACHINE_IDENTITY_INVALID")
            if machine_name in machine_names:
                raise NodeAgentError("CAPI_WORKER_MACHINE_IDENTITY_DUPLICATE")
            machine_names.add(machine_name)
            worker_machines.append(machine)

        not_ready_workers = []
        ready_worker_node_names = []
        for machine in worker_machines:
            metadata = machine.get("metadata", {})
            machine_name = metadata["name"]
            conditions = machine.get("status", {}).get("conditions", [])
            machine_ready = next(
                (
                    condition
                    for condition in conditions
                    if condition.get("type") == "Ready"
                ),
                {},
            )
            node_ref = machine.get("status", {}).get("nodeRef", {})
            node_name = node_ref.get("name") if isinstance(node_ref, dict) else None
            if (
                machine_ready.get("status") != "True"
                or not isinstance(node_name, str)
                or not node_name
                or node_ready_by_name.get(node_name) is not True
            ):
                not_ready_workers.append(machine_name)
            else:
                ready_worker_node_names.append(node_name)

        inventory_complete = (
            len(worker_machines) == expected_workers
            and len(ready_worker_node_names) == expected_workers
            and len(not_ready_workers) == 0
            and len(set(ready_worker_node_names)) == expected_workers
        )
        result.update(
            {
                "cluster_name": cluster_name,
                "namespace": namespace,
                "expected_worker_replicas": expected_workers,
                "worker_machine_count": len(worker_machines),
                "ready_worker_machine_count": len(ready_worker_node_names),
                "not_ready_worker_machine_names": not_ready_workers,
                "worker_node_names": ready_worker_node_names,
                "capi_worker_inventory_complete": inventory_complete,
                "side_effects": False,
            }
        )
        return result
    if command_type == "cluster.diagnose":
        namespace = payload.get("namespace", "default")
        if not isinstance(namespace, str) or not namespace or "/" in namespace:
            raise NodeAgentError("INVALID_NAMESPACE")
        events = kubernetes_api_get(f"/api/v1/namespaces/{namespace}/events")
        return {
            "namespace": namespace,
            "event_count": len(events.get("items", [])),
            "warning_events": [
                sanitize_kubernetes_item("events", item)
                for item in events.get("items", [])
                if item.get("type") == "Warning"
            ][:100],
        }
    if command_type == "k8s.resource.read":
        resource = payload.get("resource")
        namespace = payload.get("namespace", "default")
        if resource in {"nodes", "namespaces"}:
            data = kubernetes_api_get(f"/api/v1/{resource}")
        elif (
            resource in {"pods", "events", "services"}
            and isinstance(namespace, str)
            and namespace
            and "/" not in namespace
        ):
            data = kubernetes_api_get(f"/api/v1/namespaces/{namespace}/{resource}")
        else:
            raise NodeAgentError("RESOURCE_NOT_ALLOWLISTED")
        return {
            "resource": resource,
            "namespace": namespace,
            "items": [
                sanitize_kubernetes_item(resource, item)
                for item in data.get("items", [])[:100]
            ],
        }
    if command_type == "cluster.capi.pivot.dry-run":
        try:
            return run_capi_pivot_dry_run(payload)
        except CapiPivotError as exc:
            raise NodeAgentError(str(exc)) from exc
    if command_type == "cluster.capi.pivot.execute":
        try:
            return run_capi_pivot(payload)
        except CapiPivotError as exc:
            raise NodeAgentError(str(exc)) from exc
    if command_type == "cluster.provider.discover":
        return discover_provider_apis()
    if command_type == "cluster.manifest.validate":
        return validate_provider_manifests(payload.get("manifests"))
    if command_type == "capi.cluster.observe":
        namespace = payload.get("namespace", os.environ.get("AGENT_NAMESPACE", "default"))
        return inspect_api_group(
            "cluster.x-k8s.io",
            ["clusters", "machines", "machinedeployments"],
            namespace,
        )
    if command_type == "metal3.host.inspect":
        namespace = payload.get("namespace", os.environ.get("AGENT_NAMESPACE", "default"))
        return inspect_api_group("metal3.io", ["baremetalhosts"], namespace)
    if command_type == "host.preflight":
        disk = shutil.disk_usage("/")
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
            "root_disk_total_bytes": disk.total,
            "root_disk_free_bytes": disk.free,
            "container_runtime": {
                "docker": shutil.which("docker") is not None,
                "containerd": shutil.which("containerd") is not None,
                "crictl": shutil.which("crictl") is not None,
            },
        }
    if command_type == "cluster.plan":
        spec = payload.get("cluster_spec")
        if not isinstance(spec, dict):
            raise NodeAgentError("CLUSTER_SPEC_REQUIRED")
        cluster_name = spec.get("cluster_name")
        kubernetes_version = spec.get("kubernetes_version")
        control_plane_replicas = spec.get("control_plane_replicas")
        worker_replicas = spec.get("worker_replicas")
        if (
            not isinstance(cluster_name, str)
            or len(cluster_name) > 63
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
        ):
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        if (
            not isinstance(kubernetes_version, str)
            or not re.fullmatch(r"v1\.\d+\.\d+", kubernetes_version)
        ):
            raise NodeAgentError("INVALID_KUBERNETES_VERSION")
        if (
            not isinstance(control_plane_replicas, int)
            or isinstance(control_plane_replicas, bool)
            or control_plane_replicas < 1
            or control_plane_replicas > 9
            or control_plane_replicas % 2 == 0
        ):
            raise NodeAgentError("INVALID_CONTROL_PLANE_REPLICAS")
        if (
            not isinstance(worker_replicas, int)
            or isinstance(worker_replicas, bool)
            or worker_replicas < 0
            or worker_replicas > 1000
        ):
            raise NodeAgentError("INVALID_WORKER_REPLICAS")
        provider_versions = spec.get("provider_versions", {})
        network = spec.get("network", {})
        machine_image = spec.get("machine_image", {})
        metal3 = spec.get("metal3", {})
        validate_network_inputs(network)
        validate_metal3_hosts(metal3.get("hosts", []) if isinstance(metal3, dict) else [])
        network_profile_result = None
        if "network_profile" in spec:
            network_profile_result = validate_network_profile(spec["network_profile"])
        missing_requirements = []
        for key in [
            "cluster_api",
            "bootstrap_kubeadm",
            "control_plane_kubeadm",
            "infrastructure_metal3",
            "metal3",
            "ironic",
            "ipa",
        ]:
            if not isinstance(provider_versions, dict) or not provider_versions.get(key):
                missing_requirements.append(f"provider_versions.{key}")
        for key in ["pod_cidr", "service_cidr", "control_plane_endpoint"]:
            if not isinstance(network, dict) or not network.get(key):
                missing_requirements.append(f"network.{key}")
        for key in ["url", "checksum"]:
            if not isinstance(machine_image, dict) or not machine_image.get(key):
                missing_requirements.append(f"machine_image.{key}")
        if not isinstance(metal3, dict) or not metal3.get("namespace"):
            missing_requirements.append("metal3.namespace")
        hosts = metal3.get("hosts", []) if isinstance(metal3, dict) else []
        if not isinstance(hosts, list) or len(hosts) < control_plane_replicas + worker_replicas:
            missing_requirements.append("metal3.hosts")
        else:
            control_plane_hosts = 0
            worker_hosts = 0
            for index, host in enumerate(hosts):
                if not isinstance(host, dict):
                    missing_requirements.append(f"metal3.hosts[{index}]")
                    continue
                for key in [
                    "baremetal_host",
                    "role",
                    "bmc_credential_ref",
                    "boot_mac",
                    "root_device_hints",
                    "network_data_ref",
                ]:
                    if not host.get(key):
                        missing_requirements.append(f"metal3.hosts[{index}].{key}")
                if host.get("role") == "control-plane":
                    control_plane_hosts += 1
                elif host.get("role") == "worker":
                    worker_hosts += 1
                else:
                    missing_requirements.append(f"metal3.hosts[{index}].role")
            if control_plane_hosts < control_plane_replicas:
                missing_requirements.append("metal3.hosts.control_plane_capacity")
            if worker_hosts < worker_replicas:
                missing_requirements.append("metal3.hosts.worker_capacity")
        return {
            "plan_status": "validated_dry_run",
            "cluster_name": cluster_name,
            "kubernetes_version": kubernetes_version,
            "control_plane_replicas": control_plane_replicas,
            "worker_replicas": worker_replicas,
            "plan_hash": canonical_hash(spec),
            "network_profile": network_profile_result,
            "side_effects": False,
            "provisioning_ready": not missing_requirements,
            "missing_requirements": missing_requirements,
            "adapter_enabled": False,
            "next_required_checks": [
                "management_kubernetes_version",
                "cluster_api_provider_versions",
                "metal3_crd_versions",
                "ironic_and_ipa_versions",
                "machine_template_compatibility",
                "network_and_image_configuration",
                "baremetal_host_and_bmc_reference_validation",
            ],
        }
    raise NodeAgentError("UNSUPPORTED_COMMAND")


def kubernetes_api_get(path: str) -> dict[str, Any]:
    parsed_path = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qs(parsed_path.query, strict_parsing=True)
    if set(query) - {"continue", "limit"} or any(
        len(values) != 1 for values in query.values()
    ):
        raise NodeAgentError("KUBERNETES_QUERY_NOT_ALLOWLISTED")
    if "limit" in query and query["limit"][0] != "500":
        raise NodeAgentError("KUBERNETES_QUERY_NOT_ALLOWLISTED")
    path = parsed_path.path
    allowed_prefixes = (
        "/version",
        "/api/v1/nodes",
        "/api/v1/pods",
        "/api/v1/namespaces",
        "/api/v1/namespaces/",
        "/apis",
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/",
    )
    if not path.startswith(allowed_prefixes) or ".." in path:
        raise NodeAgentError("KUBERNETES_PATH_NOT_ALLOWLISTED")
    if "continue" in query and (
        not query["continue"][0]
        or len(query["continue"][0]) > 4096
    ):
        raise NodeAgentError("KUBERNETES_CONTINUE_TOKEN_INVALID")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    token_path = os.environ.get(
        "AGENT_SERVICE_ACCOUNT_TOKEN_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    ca_path = os.environ.get(
        "AGENT_SERVICE_ACCOUNT_CA_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )
    if not host or not os.path.isfile(token_path) or not os.path.isfile(ca_path):
        raise NodeAgentError("KUBERNETES_IN_CLUSTER_IDENTITY_UNAVAILABLE")
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()
    context = ssl.create_default_context(cafile=ca_path)
    query_string = urllib.parse.urlencode(
        {key: values[0] for key, values in query.items()}
    )
    request_url = f"https://{host}:{port}{path}"
    if query_string:
        request_url = f"{request_url}?{query_string}"
    request = urllib.request.Request(
        request_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            payload = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise NodeAgentError(f"KUBERNETES_API_HTTP_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NodeAgentError("KUBERNETES_API_UNAVAILABLE") from exc
    if len(payload) > 1024 * 1024:
        raise NodeAgentError("KUBERNETES_RESPONSE_TOO_LARGE")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeAgentError("KUBERNETES_RESPONSE_INVALID_JSON") from exc
    if not isinstance(result, dict):
        raise NodeAgentError("KUBERNETES_RESPONSE_INVALID_SHAPE")
    return result


class ControlPlaneClient:
    def __init__(self, base_url: str, node_id: str, token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.token = token
        self.timeout = timeout
        self.deployment_checkpoints: dict[str, dict[str, Any]] = {}

    def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        lease_token: str = "",
        method: str = "POST",
    ) -> dict[str, Any]:
        if method not in {"GET", "POST"}:
            raise NodeAgentError("UNSUPPORTED_CONTROL_PLANE_METHOD")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if lease_token:
            headers["X-Agent-Lease"] = lease_token
        request_path = path
        request_data = None
        if method == "GET":
            query = urllib.parse.urlencode(payload or {})
            if query:
                request_path = f"{path}?{query}"
        else:
            request_data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{request_path}",
            data=request_data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise NodeAgentError(f"CONTROL_PLANE_HTTP_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NodeAgentError("CONTROL_PLANE_UNAVAILABLE") from exc

    def restore_deployment_checkpoint(self, deployment_id: str) -> dict[str, Any]:
        if not deployment_id:
            raise NodeAgentError("DEPLOYMENT_ID_REQUIRED")
        checkpoint = self.request(
            f"/api/v1/agent-runtime/deployments/{deployment_id}/checkpoint/",
            {"node_id": self.node_id},
            method="GET",
        )
        if checkpoint.get("deployment_id") != deployment_id:
            raise NodeAgentError("CHECKPOINT_DEPLOYMENT_MISMATCH")
        if checkpoint.get("resume_available") is True:
            if not isinstance(checkpoint.get("data"), dict):
                raise NodeAgentError("INVALID_DEPLOYMENT_CHECKPOINT")
            if (
                not isinstance(checkpoint.get("sequence"), int)
                or isinstance(checkpoint.get("sequence"), bool)
                or checkpoint.get("sequence") < 1
            ):
                raise NodeAgentError("INVALID_DEPLOYMENT_CHECKPOINT")
            if checkpoint.get("generation") != 1:
                raise NodeAgentError("CHECKPOINT_GENERATION_UNSUPPORTED")
        self.deployment_checkpoints[deployment_id] = checkpoint
        return checkpoint

    def save_deployment_checkpoint(
        self,
        deployment_id: str,
        generation: int,
        sequence: int,
        phase: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_phases = {
            "target_agent_bootstrap",
            "capi_pivot_preflight",
            "capi_pivot_verified",
            "worker_provisioning",
            "worker_pool_verified",
            "eph_reclaim_pending",
            "completed",
        }
        if (
            not deployment_id
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation != 1
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or phase not in allowed_phases
        ):
            raise NodeAgentError("INVALID_DEPLOYMENT_CHECKPOINT")
        if not isinstance(data, dict) or contains_forbidden_fields(data):
            raise NodeAgentError("INVALID_DEPLOYMENT_CHECKPOINT")
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 32768:
            raise NodeAgentError("INVALID_DEPLOYMENT_CHECKPOINT")
        result = self.request(
            f"/api/v1/agent-runtime/deployments/{deployment_id}/checkpoint/",
            {
                "node_id": self.node_id,
                "generation": generation,
                "sequence": sequence,
                "phase": phase,
                "data": data,
            },
        )
        self.deployment_checkpoints[deployment_id] = {
            "deployment_id": deployment_id,
            "generation": generation,
            "sequence": sequence,
            "phase": phase,
            "data": data,
            "resume_available": True,
        }
        return result

    def heartbeat(self, node_type: str) -> dict[str, Any]:
        capabilities = (
            [
                "node.health",
                "host.preflight",
                "cluster.plan",
                "cluster.health",
                "cluster.observe",
                "cluster.diagnose",
                "cluster.postflight",
                "k8s.resource.read",
                "capi.cluster.observe",
                "metal3.host.inspect",
                "cluster.provider.discover",
                "cluster.manifest.validate",
                "cluster.capi.pivot.source.preflight",
                "cluster.capi.pivot.dry-run",
                "cluster.capi.pivot.execute",
            ]
            if node_type == "eph_node"
            else [
                "node.health",
                "cluster.health",
                "cluster.observe",
                "cluster.diagnose",
                "cluster.postflight",
                "cluster.control-plane.verify",
                "cluster.capi.pivot.target.preflight",
                "k8s.resource.read",
            ]
        )
        return self.request(
            "/api/v1/agent-runtime/heartbeat/",
            {
                "node_id": self.node_id,
                "agent_version": "0.1.0",
                "capabilities": capabilities,
            },
        )

    def poll(self) -> dict[str, Any]:
        return self.request(
            "/api/v1/agent-runtime/commands/poll/",
            {"node_id": self.node_id},
        )


    def renew_lease(self, command_id: str, lease_token: str) -> dict[str, Any]:
        return self.request(
            f"/api/v1/agent-runtime/commands/{command_id}/lease/renew/",
            {"node_id": self.node_id},
            lease_token=lease_token,
        )

    def submit_result(
        self,
        command_id: str,
        lease_token: str,
        outcome: str,
        result: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            f"/api/v1/agent-runtime/commands/{command_id}/result/",
            {
                "node_id": self.node_id,
                "outcome": outcome,
                "result": result,
                "error": error,
            },
            lease_token=lease_token,
        )


def touch_health_files(readiness: bool = False) -> None:
    Path(os.environ.get("AGENT_LIVENESS_FILE", "/tmp/agent-liveness")).touch()
    if readiness:
        Path(os.environ.get("AGENT_READINESS_FILE", "/tmp/agent-readiness")).touch()


def health_file_is_fresh(name: str, max_age_seconds: int = 45) -> bool:
    default_path = (
        "/tmp/agent-liveness"
        if name == "AGENT_LIVENESS_FILE"
        else "/tmp/agent-readiness"
    )
    path = Path(os.environ.get(name, default_path))
    if not path.is_file():
        return False
    return time.time() - path.stat().st_mtime <= max_age_seconds


def execute_command_with_lease(
    client: ControlPlaneClient,
    command: dict[str, Any],
    node_type: str,
    renew_interval: int = 30,
) -> dict[str, Any]:
    if not isinstance(renew_interval, (int, float)) or renew_interval <= 0:
        raise NodeAgentError("INVALID_LEASE_RENEW_INTERVAL")
    command_id = command.get("id", "")
    lease_token = command.get("lease_token", "")
    if not command_id or not lease_token:
        raise NodeAgentError("COMMAND_LEASE_REQUIRED")

    stop_event = threading.Event()
    renewal_errors: list[NodeAgentError] = []

    def renew_until_stopped() -> None:
        while not stop_event.wait(renew_interval):
            try:
                client.renew_lease(command_id, lease_token)
            except NodeAgentError as exc:
                renewal_errors.append(exc)
                stop_event.set()
            except Exception:
                renewal_errors.append(
                    NodeAgentError("COMMAND_LEASE_RENEWAL_FAILED")
                )
                stop_event.set()

    renewer = threading.Thread(
        target=renew_until_stopped,
        name=f"agent-lease-{command_id}",
        daemon=True,
    )
    renewer.start()
    try:
        result = execute_command(command, node_type)
    finally:
        stop_event.set()
        renewer.join()

    if renewal_errors:
        raise NodeAgentError("COMMAND_LEASE_LOST") from renewal_errors[0]
    return result


def run_once(client: ControlPlaneClient, node_type: str) -> bool:
    heartbeat = client.heartbeat(node_type)
    if node_type == "target_cluster":
        for deployment in heartbeat.get("active_deployments", []):
            deployment_id = deployment.get("deployment_id")
            if (
                isinstance(deployment_id, str)
                and deployment_id
                and deployment_id not in client.deployment_checkpoints
            ):
                checkpoint = client.restore_deployment_checkpoint(deployment_id)
                print(
                    json.dumps(
                        {
                            "event": "deployment_checkpoint_restored",
                            "deployment_id": deployment_id,
                            "resume_available": checkpoint.get("resume_available", False),
                            "phase": checkpoint.get("phase"),
                            "sequence": checkpoint.get("sequence", 0),
                        }
                    ),
                    flush=True,
                )
    envelope = client.poll()
    command = envelope.get("command")
    if not command:
        touch_health_files(readiness=True)
        return False
    lease_token = command.get("lease_token", "")
    command_id = command.get("id", "")
    try:
        result = execute_command_with_lease(client, command, node_type)
        client.submit_result(command_id, lease_token, "succeeded", result)
    except NodeAgentError as exc:
        client.submit_result(
            command_id,
            lease_token,
            "failed",
            {},
            {"code": str(exc), "retryable": False},
        )
    touch_health_files(readiness=True)
    return True



class EPHNodeAgent:
    node_type = "eph_node"

    def __init__(self, client: ControlPlaneClient):
        self.client = client

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        return execute_command(command, self.node_type)

    def run_once(self) -> bool:
        return run_once(self.client, self.node_type)


class TargetClusterAgent:
    node_type = "target_cluster"

    def __init__(self, client: ControlPlaneClient):
        self.client = client

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        return execute_command(command, self.node_type)

    def run_once(self) -> bool:
        return run_once(self.client, self.node_type)


def create_agent(client: ControlPlaneClient, node_type: str):
    if node_type == EPHNodeAgent.node_type:
        return EPHNodeAgent(client)
    if node_type == TargetClusterAgent.node_type:
        return TargetClusterAgent(client)
    raise NodeAgentError("INVALID_AGENT_ROLE")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent Studio remote node agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(0 if health_file_is_fresh("AGENT_LIVENESS_FILE") else 1)
    if args.readiness_check:
        raise SystemExit(0 if health_file_is_fresh("AGENT_READINESS_FILE") else 1)
    base_url = os.environ.get("AGENT_CONTROL_URL", "").strip()
    node_id = os.environ.get("AGENT_NODE_ID", "").strip()
    token = os.environ.get("AGENT_TOKEN", "").strip()
    node_type = os.environ.get("AGENT_NODE_TYPE", "").strip()
    if not base_url or not node_id or not token:
        raise SystemExit("AGENT_CONTROL_URL, AGENT_NODE_ID and AGENT_TOKEN are required.")
    if node_type not in {"eph_node", "target_cluster"}:
        raise SystemExit("AGENT_NODE_TYPE must be eph_node or target_cluster.")
    if args.poll_seconds < 1 or args.poll_seconds > 300:
        raise SystemExit("--poll-seconds must be between 1 and 300.")
    if not base_url.startswith("https://") and os.environ.get("APP_ENV") == "production":
        raise SystemExit("Production node agents require HTTPS.")
    client = ControlPlaneClient(base_url, node_id, token)
    runtime = create_agent(client, node_type)
    for _iteration in count():
        try:
            processed = runtime.run_once()
            if args.once:
                break
            if not processed:
                time.sleep(args.poll_seconds)
        except NodeAgentError as exc:
            touch_health_files()
            print(json.dumps({"event": "control_plane_error", "code": str(exc)}), flush=True)
            if args.once:
                raise SystemExit(1) from exc
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
