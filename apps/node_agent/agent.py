import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from itertools import count
from pathlib import Path
from typing import Any


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
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "k8s.resource.read",
            "capi.cluster.observe",
            "metal3.host.inspect",
            "cluster.provider.discover",
            "cluster.manifest.validate",
            "cluster.capi.pivot.source.preflight",
        },
        "target_cluster": {
            "node.health",
            "cluster.health",
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "cluster.control-plane.verify",
            "cluster.capi.pivot.target.preflight",
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
        inventory = []
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
                resource = api_resources.get(resource_name)
                if resource is None:
                    missing_resources.append(f"{group_name}/{resource_name}")
                    continue
                path = f"/apis/{group_name}/{version}/{resource_name}"
                document = kubernetes_api_get(path)
                items = document.get("items", [])
                if not isinstance(items, list):
                    raise NodeAgentError("CAPI_SOURCE_INVENTORY_INVALID")
                resource_key = f"{group_name}/{resource_name}"
                resource_counts[resource_key] = len(items)
                if document.get("metadata", {}).get("continue") or len(items) > 100:
                    pagination_required.append(resource_key)
                for item in items[:100]:
                    metadata = item.get("metadata", {})
                    owner_references = metadata.get("ownerReferences", [])
                    inventory.append(
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
                            "cluster_name": metadata.get("labels", {}).get(
                                "cluster.x-k8s.io/cluster-name"
                            ),
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
        inventory_truncated = len(inventory) > 200
        inventory_complete = (
            not missing_resources
            and not pagination_required
            and not inventory_truncated
        )
        source_cluster_matches = [
            item
            for item in inventory
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
            ):
                source_cluster_identity = {
                    "name": cluster_name,
                    "namespace": source_cluster["namespace"],
                    "uid": source_cluster["uid"],
                    "api_version": source_cluster.get("api_version"),
                }
        blockers = []
        if missing_resources:
            blockers.append("required_source_capi_metal3_apis_missing")
        if not controllers_ready:
            blockers.append("source_management_controllers_not_ready")
        if pagination_required:
            blockers.append("source_inventory_pagination_required")
        if inventory_truncated:
            blockers.append("source_inventory_truncated")
        if not inventory:
            blockers.append("source_inventory_empty")
        if not source_cluster_matches:
            blockers.append("requested_source_cluster_not_found")
        elif source_cluster_identity is None:
            blockers.append("requested_source_cluster_identity_invalid")
        elif len(source_cluster_matches) > 1:
            blockers.append("requested_source_cluster_identity_ambiguous")
        return {
            "source_management_plane_ready": (
                inventory_complete
                and controllers_ready
                and bool(inventory)
                and source_cluster_identity is not None
                and len(source_cluster_matches) == 1
            ),
            "source_cluster_identity": source_cluster_identity,
            "cluster_name": cluster_name,
            "provider_versions": provider_versions,
            "resource_counts": resource_counts,
            "inventory": inventory[:200],
            "inventory_truncated": inventory_truncated,
            "missing_resources": missing_resources[:100],
            "pagination_required": sorted(set(pagination_required))[:100],
            "controllers": controller_states,
            "blockers": blockers,
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
    if command_type == "cluster.postflight":
        nodes = kubernetes_api_get("/api/v1/nodes")
        items = nodes.get("items", [])
        not_ready = []
        for item in items:
            conditions = item.get("status", {}).get("conditions", [])
            ready = next(
                (
                    condition
                    for condition in conditions
                    if condition.get("type") == "Ready"
                ),
                {},
            )
            if ready.get("status") != "True":
                not_ready.append(item.get("metadata", {}).get("name"))
        return {
            "node_count": len(items),
            "ready": len(items) > 0 and not not_ready,
            "not_ready_nodes": not_ready,
        }
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
    allowed_prefixes = (
        "/version",
        "/api/v1/nodes",
        "/api/v1/namespaces",
        "/api/v1/namespaces/",
        "/apis",
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/",
    )
    if not path.startswith(allowed_prefixes) or ".." in path or "?" in path:
        raise NodeAgentError("KUBERNETES_PATH_NOT_ALLOWLISTED")
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
    request = urllib.request.Request(
        f"https://{host}:{port}{path}",
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
        result = execute_command(command, node_type)
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
    for _iteration in count():
        try:
            processed = run_once(client, node_type)
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
