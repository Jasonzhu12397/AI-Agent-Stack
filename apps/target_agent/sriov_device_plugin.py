from __future__ import annotations

import json
import re
from typing import Any


class SriovDevicePluginConfigError(ValueError):
    pass


_BDF = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\\.[0-7]$")
_RESOURCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_PREFIX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$")
_VENDOR = re.compile(r"^[0-9a-fA-F]{4}$")


def validate_sriov_device_plugin_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_POOLS_REQUIRED")
    normalized: dict[str, Any] = {}
    seen_configmaps: dict[str, dict[str, Any]] = {}
    devices_by_configmap: dict[str, set[str]] = {}
    device_configmap: dict[str, str] = {}

    for pool_name, pool in value.items():
        if not isinstance(pool_name, str) or not re.fullmatch(r"pool[0-9]+", pool_name):
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_POOL_NAME_INVALID")
        if not isinstance(pool, dict):
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_POOL_INVALID")
        selectors = pool.get("node_selectors")
        if not isinstance(selectors, dict) or not selectors:
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_NODE_SELECTORS_REQUIRED")
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(item, str)
            or not item
            for key, item in selectors.items()
        ):
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_NODE_SELECTORS_INVALID")
        configmap_name = pool.get("sriov_network_device_plugin_configmap_name")
        if not isinstance(configmap_name, str) or not re.fullmatch(
            r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", configmap_name
        ):
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_CONFIGMAP_NAME_INVALID")
        raw_config = pool.get("sriov_network_device_plugin_configmap_data")
        if isinstance(raw_config, str):
            try:
                config = json.loads(raw_config)
            except ValueError as exc:
                raise SriovDevicePluginConfigError(
                    "SRIOV_DEVICE_PLUGIN_CONFIGMAP_JSON_INVALID"
                ) from exc
        else:
            config = raw_config
        resources = config.get("resourceList") if isinstance(config, dict) else None
        if not isinstance(resources, list) or not resources:
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_RESOURCE_LIST_REQUIRED")
        normalized_resources = []
        pool_resource_ids: set[tuple[str, str]] = set()
        pool_devices: set[str] = set()
        for resource in resources:
            if not isinstance(resource, dict):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_RESOURCE_INVALID")
            name = resource.get("resourceName")
            prefix = resource.get("resourcePrefix")
            rdma = resource.get("isRdma")
            device_selectors = resource.get("selectors")
            if (
                not isinstance(name, str)
                or not _RESOURCE.fullmatch(name)
                or not isinstance(prefix, str)
                or not _PREFIX.fullmatch(prefix)
                or not isinstance(rdma, bool)
                or not isinstance(device_selectors, dict)
            ):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_RESOURCE_INVALID")
            identity = (prefix, name)
            if identity in pool_resource_ids:
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_RESOURCE_DUPLICATE")
            pool_resource_ids.add(identity)
            vendors = device_selectors.get("vendors")
            drivers = device_selectors.get("drivers")
            root_devices = device_selectors.get("rootDevices")
            selector_rdma = device_selectors.get("isRdma")
            if (
                not isinstance(vendors, list)
                or not vendors
                or any(not isinstance(item, str) or not _VENDOR.fullmatch(item) for item in vendors)
                or not isinstance(drivers, list)
                or not drivers
                or any(not isinstance(item, str) or not item for item in drivers)
                or not isinstance(root_devices, list)
                or not root_devices
                or any(not isinstance(item, str) or not _BDF.fullmatch(item) for item in root_devices)
                or not isinstance(selector_rdma, bool)
                or selector_rdma != rdma
            ):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_SELECTORS_INVALID")
            normalized_bdfs = [item.lower() for item in root_devices]
            if len(set(normalized_bdfs)) != len(normalized_bdfs):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_ROOT_DEVICE_DUPLICATE")
            if pool_devices.intersection(normalized_bdfs):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_ROOT_DEVICE_DUPLICATE")
            pool_devices.update(normalized_bdfs)
            normalized_resources.append({
                "resourceName": name,
                "resourcePrefix": prefix,
                "isRdma": rdma,
                "selectors": {
                    "vendors": [item.lower() for item in vendors],
                    "drivers": list(drivers),
                    "rootDevices": normalized_bdfs,
                    "isRdma": selector_rdma,
                },
            })
        tolerations = pool.get("tolerations", [])
        if not isinstance(tolerations, list):
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_TOLERATIONS_INVALID")
        normalized_tolerations = []
        for toleration in tolerations:
            if not isinstance(toleration, dict):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_TOLERATION_INVALID")
            effect = toleration.get("effect")
            key = toleration.get("key")
            operator = toleration.get("operator")
            toleration_value = toleration.get("value")
            if (
                effect not in {"NoSchedule", "PreferNoSchedule", "NoExecute"}
                or not isinstance(key, str)
                or not key
                or operator not in {"Equal", "Exists"}
                or (operator == "Equal" and (not isinstance(toleration_value, str) or not toleration_value))
                or (operator == "Exists" and toleration_value not in (None, ""))
            ):
                raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_TOLERATION_INVALID")
            normalized_tolerations.append({
                "effect": effect,
                "key": key,
                "operator": operator,
                **({"value": toleration_value} if operator == "Equal" else {}),
            })
        normalized_config = {"resourceList": normalized_resources}
        previous_config = seen_configmaps.get(configmap_name)
        if previous_config is not None and previous_config != normalized_config:
            raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_CONFIGMAP_CONTENT_CONFLICT")
        seen_configmaps[configmap_name] = normalized_config
        devices = set(
            device
            for resource in normalized_resources
            for device in resource["selectors"]["rootDevices"]
        )
        previous_devices = devices_by_configmap.setdefault(configmap_name, set())
        if previous_config is None:
            for device in devices:
                if device in device_configmap and device_configmap[device] != configmap_name:
                    raise SriovDevicePluginConfigError(
                        "SRIOV_DEVICE_PLUGIN_ROOT_DEVICE_ASSIGNED_TO_MULTIPLE_CONFIGMAPS"
                    )
                device_configmap[device] = configmap_name
        previous_devices.update(devices)
        normalized[pool_name] = {
            "node_selectors": dict(selectors),
            "tolerations": normalized_tolerations,
            "sriov_network_device_plugin_configmap_name": configmap_name,
            "sriov_network_device_plugin_configmap_data": normalized_config,
        }
    return normalized


def render_sriov_device_plugin_configmap(
    pool: dict[str, Any],
    *,
    namespace: str,
    deployment_id: str,
    cluster_name: str,
    target_cluster_uid: str,
) -> dict[str, Any]:
    if not isinstance(pool, dict):
        raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_POOL_INVALID")
    name = pool.get("sriov_network_device_plugin_configmap_name")
    config = pool.get("sriov_network_device_plugin_configmap_data")
    if not isinstance(name, str) or not isinstance(config, dict):
        raise SriovDevicePluginConfigError("SRIOV_DEVICE_PLUGIN_CONFIGMAP_INVALID")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "target-agent",
                "ai-agent-stack.io/deployment-id": deployment_id,
                "ai-agent-stack.io/cluster-name": cluster_name,
            },
            "annotations": {
                "ai-agent-stack.io/target-cluster-uid": target_cluster_uid,
            },
        },
        "data": {
            "config.json": json.dumps(config, sort_keys=True, separators=(",", ":")),
        },
    }
