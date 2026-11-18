from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from .metal3_skill import Metal3SkillError

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"
_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def _valid_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value) or len(value) > 63:
        raise Metal3SkillError(code)
    return value


def _validate_bmc_address(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise Metal3SkillError("METAL3_BMC_ADDRESS_INVALID")
    parsed = urlsplit(value)
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(char in value for char in ("\r", "\n"))
    ):
        raise Metal3SkillError("METAL3_BMC_ADDRESS_INVALID")
    return value


def render_bmh_manifests(hosts: Any, namespace: str = "metal3") -> list[dict[str, Any]]:
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 2500:
        raise Metal3SkillError("METAL3_BMH_HOST_PLAN_INVALID")
    normalized = []
    seen = set()
    for host in hosts:
        if not isinstance(host, dict):
            raise Metal3SkillError("METAL3_BMH_HOST_PLAN_INVALID")
        name = _valid_name(host.get("name"), "METAL3_BMH_NAME_INVALID")
        pool = _valid_name(host.get("node_pool_name"), "METAL3_BMH_POOL_INVALID")
        boot_mac = host.get("boot_mac_address")
        secret_ref = _valid_name(host.get("secret_ref"), "METAL3_BMH_SECRET_REFERENCE_INVALID")
        if not isinstance(boot_mac, str) or not _MAC_PATTERN.fullmatch(boot_mac):
            raise Metal3SkillError("METAL3_BMH_BOOT_MAC_INVALID")
        if name in seen:
            raise Metal3SkillError("METAL3_BMH_HOST_DUPLICATE")
        seen.add(name)
        online = host.get("online", False)
        disable_verification = host.get("disable_certificate_verification", True)
        if not isinstance(online, bool) or not isinstance(disable_verification, bool):
            raise Metal3SkillError("METAL3_BMH_HOST_FLAGS_INVALID")
        item = {
            "name": name,
            "namespace": namespace,
            "node_pool_name": pool,
            "bmc_address": _validate_bmc_address(host.get("bmc_address")),
            "boot_mac_address": boot_mac.lower(),
            "secret_ref": secret_ref,
            "online": online,
            "disable_certificate_verification": disable_verification,
        }
        for optional in ("node_type", "pcc_mm_pod", "pcc_sm_pod", "root_device_hint_model"):
            if optional in host:
                value = host[optional]
                if not isinstance(value, str) or not value or len(value) > 253:
                    raise Metal3SkillError("METAL3_BMH_OPTIONAL_FIELD_INVALID")
                item[optional] = value
        annotations = host.get("annotations", {})
        if not isinstance(annotations, dict) or len(annotations) > 32:
            raise Metal3SkillError("METAL3_BMH_ANNOTATIONS_INVALID")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 253
            or len(value) > 1024
            for key, value in annotations.items()
        ):
            raise Metal3SkillError("METAL3_BMH_ANNOTATIONS_INVALID")
        if annotations:
            item["annotations"] = annotations
        normalized.append(item)
    try:
        rendered = _environment().get_template("bmh/bmh-template.yaml.j2").render(
            hosts=normalized
        )
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except (TemplateError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise Metal3SkillError("METAL3_BMH_TEMPLATE_RENDER_FAILED") from exc
    if len(documents) != len(normalized):
        raise Metal3SkillError("METAL3_BMH_TEMPLATE_DOCUMENT_COUNT_MISMATCH")
    return documents


def _render_data_template(
    cluster_name: str,
    namespace: str,
    template_name: str,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Metal3SkillError("CAPI_METAL3_DATA_TEMPLATE_REQUIRED")
    meta_data = value.get("meta_data")
    network_data = value.get("network_data")
    if not isinstance(meta_data, dict) or not isinstance(network_data, dict):
        raise Metal3SkillError("CAPI_METAL3_DATA_TEMPLATE_CONTENT_INVALID")
    if not {"links", "networks"}.issubset(network_data):
        raise Metal3SkillError("CAPI_METAL3_NETWORK_DATA_INCOMPLETE")
    document = {
        "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta2",
        "kind": "Metal3DataTemplate",
        "metadata": {
            "name": template_name,
            "namespace": namespace,
        },
        "spec": {
            "clusterName": cluster_name,
            "metaData": meta_data,
            "networkData": network_data,
        },
    }
    if len(yaml.safe_dump(document, sort_keys=False).encode("utf-8")) > 64 * 1024:
        raise Metal3SkillError("CAPI_METAL3_DATA_TEMPLATE_TOO_LARGE")
    return document


def render_capi_metal3_manifests(cluster_spec: Any) -> list[dict[str, Any]]:
    if not isinstance(cluster_spec, dict):
        raise Metal3SkillError("CAPI_CLUSTER_SPEC_INVALID")
    cluster = dict(cluster_spec)
    cluster["name"] = _valid_name(cluster.get("name"), "CAPI_CLUSTER_NAME_INVALID")
    cluster["namespace"] = _valid_name(
        cluster.get("namespace", "metal3"),
        "CAPI_CLUSTER_NAMESPACE_INVALID",
    )
    if cluster.get("infrastructure_provider", "metal3") != "metal3":
        raise Metal3SkillError("CAPI_INFRASTRUCTURE_PROVIDER_UNSUPPORTED")
    endpoint = cluster.get("control_plane_endpoint")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 253:
        raise Metal3SkillError("CAPI_CONTROL_PLANE_ENDPOINT_INVALID")
    try:
        ipaddress.ip_address(endpoint)
    except ValueError:
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*",
            endpoint,
        ):
            raise Metal3SkillError("CAPI_CONTROL_PLANE_ENDPOINT_INVALID") from None
    port = cluster.get("control_plane_port", 6443)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise Metal3SkillError("CAPI_CONTROL_PLANE_PORT_INVALID")
    cluster["control_plane_port"] = port
    version = cluster.get("k8s_version", "v1.31.0")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise Metal3SkillError("CAPI_KUBERNETES_VERSION_INVALID")
    cluster["k8s_version"] = version
    image_url = cluster.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://", "oci://")):
        raise Metal3SkillError("CAPI_MACHINE_IMAGE_URL_REQUIRED")
    image_parsed = urlsplit(image_url)
    if image_parsed.scheme in {"http", "https"} and (
        not image_parsed.hostname or image_parsed.username or image_parsed.password
    ):
        raise Metal3SkillError("CAPI_MACHINE_IMAGE_URL_INVALID")
    cluster["image_url"] = image_url
    control_plane_count = cluster.get("control_plane_count", 3)
    if (
        not isinstance(control_plane_count, int)
        or isinstance(control_plane_count, bool)
        or control_plane_count not in {1, 3, 5, 7}
    ):
        raise Metal3SkillError("CAPI_CONTROL_PLANE_COUNT_INVALID")
    cluster["control_plane_count"] = control_plane_count
    pools = cluster.get("worker_pools", [])
    if not isinstance(pools, list) or len(pools) > 64:
        raise Metal3SkillError("CAPI_WORKER_POOLS_INVALID")
    normalized_pools = []
    seen_pools = set()
    for pool in pools:
        if not isinstance(pool, dict):
            raise Metal3SkillError("CAPI_WORKER_POOL_INVALID")
        pool = dict(pool)
        pool["name"] = _valid_name(pool.get("name"), "CAPI_WORKER_POOL_NAME_INVALID")
        if pool["name"] in seen_pools:
            raise Metal3SkillError("CAPI_WORKER_POOL_DUPLICATE")
        seen_pools.add(pool["name"])
        count = pool.get("count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 2500:
            raise Metal3SkillError("CAPI_WORKER_POOL_COUNT_INVALID")
        labels = pool.get("node_labels", [])
        if not isinstance(labels, list) or len(labels) > 32 or any(
            not isinstance(label, str) or len(label) > 253 for label in labels
        ):
            raise Metal3SkillError("CAPI_WORKER_POOL_LABELS_INVALID")
        pool["node_labels"] = labels
        pool["data_template"] = _render_data_template(
            cluster["name"],
            cluster["namespace"],
            f"{cluster['name']}-{pool['name']}-template",
            pool.get("data_template"),
        )
        normalized_pools.append(pool)
    cluster["worker_pools"] = normalized_pools
    control_plane_data_template = _render_data_template(
        cluster["name"],
        cluster["namespace"],
        f"{cluster['name']}-controlplane-template",
        cluster.get("control_plane_data_template"),
    )
    try:
        rendered = _environment().get_template("capi/cluster-template.yaml.j2").render(
            cluster=cluster
        )
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except (TemplateError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise Metal3SkillError("CAPI_METAL3_TEMPLATE_RENDER_FAILED") from exc
    if not documents:
        raise Metal3SkillError("CAPI_METAL3_TEMPLATE_EMPTY")
    data_templates = [control_plane_data_template] + [
        pool["data_template"] for pool in normalized_pools
    ]
    cluster_documents = [
        item for item in documents if item["kind"] in {"Cluster", "Metal3Cluster"}
    ]
    remaining_documents = [
        item for item in documents if item["kind"] not in {"Cluster", "Metal3Cluster"}
    ]
    return cluster_documents + data_templates + remaining_documents
