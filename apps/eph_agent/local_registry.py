"""Render the EPH local registry and BMC services as Kubernetes workloads.

The registry, vbmc, sushy-tools, and httpd-infra all run as Pods in the EPH
single-node cluster. Images arrive in Live ISO archives, are imported into containerd, and
are published only to the registry on the EPH provisioning NIC. No Docker
Compose stack, virtual machine, or external registry is involved.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .metal3_skill import Metal3SkillError

_DIGEST_IMAGE = re.compile(
    r"^(?P<endpoint>[A-Za-z0-9.-]+:[0-9]{1,5})/(?P<repository>[a-z0-9._/-]+)@sha256:[a-f0-9]{64}$"
)
_ENDPOINT = re.compile(r"^[A-Za-z0-9.-]+:[0-9]{1,5}$")
_REQUIRED_SERVICES = ("vbmc", "sushy-tools", "httpd-infra")
_NAMESPACE = "ai-agent-system"


def _pinned_image(value: Any, code: str) -> tuple[str, str]:
    if not isinstance(value, str) or not (match := _DIGEST_IMAGE.fullmatch(value)):
        raise Metal3SkillError(code)
    port = int(match.group("endpoint").rsplit(":", 1)[1])
    if not 1 <= port <= 65535:
        raise Metal3SkillError(code)
    return match.group("endpoint"), match.group("repository")


def _absolute_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise Metal3SkillError(code)
    return str(Path(value))


def _valid_endpoint(value: Any) -> str:
    if not isinstance(value, str) or not _ENDPOINT.fullmatch(value):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
    if not 1 <= int(value.rsplit(":", 1)[1]) <= 65535:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
    return value


def target_registry_config(endpoint: Any, pause_image: str | None = None) -> dict[str, str]:
    endpoint = _valid_endpoint(endpoint)
    if pause_image is not None and not re.fullmatch(
        rf"{re.escape(endpoint)}/ai-agent-stack/pause:[A-Za-z0-9._-]+", pause_image
    ):
        raise Metal3SkillError("EPH_LOCAL_PAUSE_IMAGE_INVALID")
    sandbox = f'  sandbox_image = "{pause_image}"\n' if pause_image is not None else ""
    return {
        "endpoint": endpoint,
        "image_repository": f"{endpoint}/ai-agent-stack",
        "dns_image_repository": f"{endpoint}/ai-agent-stack/coredns",
        "pause_image": pause_image or "",
        "containerd_config": (
            "version = 2\n\n"
            + ('[plugins."io.containerd.grpc.v1.cri"]\n' + sandbox if sandbox else "")
            + '[plugins."io.containerd.grpc.v1.cri".registry]\n'
            '  config_path = "/etc/containerd/certs.d"\n'
        ),
        "hosts_toml": (
            f'server = "http://{endpoint}"\n\n'
            f'[host."http://{endpoint}"]\n'
            '  capabilities = ["pull", "resolve", "push"]\n'
            "  skip_verify = true\n"
        ),
    }


def _deployment(name: str, container: dict[str, Any], pod: dict[str, Any]) -> dict[str, Any]:
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "eph-bootstrap"}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": _NAMESPACE, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [container],
                    "restartPolicy": "Always",
                    **pod,
                },
            },
        },
    }


def render_local_registry(
    value: Any,
    *,
    node_ip: str,
    agent_image: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_REQUIRED")
    endpoint = _valid_endpoint(value.get("endpoint", f"{node_ip}:5000"))
    agent_endpoint, agent_repository = _pinned_image(
        agent_image, "EPH_BOOTSTRAP_AGENT_IMAGE_INVALID"
    )
    if agent_endpoint != endpoint:
        raise Metal3SkillError("EPH_AGENT_IMAGE_MUST_USE_LOCAL_REGISTRY")
    target_image = value.get("target_agent_image")
    target_endpoint, target_repository = _pinned_image(
        target_image, "EPH_TARGET_AGENT_IMAGE_INVALID"
    )
    if target_endpoint != endpoint:
        raise Metal3SkillError("EPH_TARGET_AGENT_IMAGE_MUST_USE_LOCAL_REGISTRY")

    registry_image = value.get("registry_image")
    if not isinstance(registry_image, dict):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_REQUIRED")
    registry_archive = _absolute_path(
        registry_image.get("archive"), "EPH_LOCAL_REGISTRY_ARCHIVE_INVALID"
    )
    registry_reference = registry_image.get("reference")
    if not isinstance(registry_reference, str) or not re.fullmatch(
        r"[a-z0-9._/-]+:[A-Za-z0-9._-]+", registry_reference
    ):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_INVALID")
    data_dir = _absolute_path(
        value.get("data_dir", "/var/lib/ai-agent-stack/registry"),
        "EPH_LOCAL_REGISTRY_DATA_DIR_INVALID",
    )

    archives = value.get("images")
    if not isinstance(archives, list) or not 1 <= len(archives) <= 256:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGES_REQUIRED")
    normalized_archives: list[dict[str, str]] = []
    repositories: set[str] = set()
    for item in archives:
        if not isinstance(item, dict):
            raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_ENTRY_INVALID")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not re.fullmatch(
            r"[a-z0-9._/-]+:[A-Za-z0-9._-]+", source
        ):
            raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_SOURCE_INVALID")
        if not isinstance(target, str) or not target.startswith(endpoint + "/"):
            raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_TARGET_INVALID")
        repository = target.split("/", 1)[1].rsplit(":", 1)[0]
        if repository in repositories:
            raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_DUPLICATE")
        repositories.add(repository)
        normalized_archives.append(
            {
                "archive": _absolute_path(
                    item.get("archive"), "EPH_LOCAL_REGISTRY_ARCHIVE_INVALID"
                ),
                "source": source,
                "target": target,
            }
        )

    services = value.get("services")
    if not isinstance(services, dict) or not set(_REQUIRED_SERVICES).issubset(services):
        raise Metal3SkillError("EPH_LOCAL_BMC_SERVICES_REQUIRED")
    required_repositories = {agent_repository, target_repository}
    service_documents = []
    service_images = []
    for name in _REQUIRED_SERVICES:
        service = services[name]
        if not isinstance(service, dict) or set(service) - {"container", "pod"}:
            raise Metal3SkillError("EPH_LOCAL_BMC_SERVICE_INVALID")
        container = service.get("container")
        pod = service.get("pod", {})
        if not isinstance(container, dict) or not isinstance(pod, dict):
            raise Metal3SkillError("EPH_LOCAL_BMC_SERVICE_INVALID")
        container = dict(container)
        service_endpoint, repository = _pinned_image(
            container.get("image"), "EPH_LOCAL_BMC_SERVICE_IMAGE_INVALID"
        )
        if service_endpoint != endpoint:
            raise Metal3SkillError("EPH_LOCAL_BMC_SERVICE_IMAGE_INVALID")
        required_repositories.add(repository)
        service_images.append(container["image"])
        container.update({"name": name, "imagePullPolicy": "IfNotPresent"})
        service_documents.append(_deployment(name, container, dict(pod)))
    if not required_repositories.issubset(repositories):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_SET_INCOMPLETE")

    registry_container = {
        "name": "registry",
        "image": registry_reference,
        "imagePullPolicy": "Never",
        "ports": [{"name": "registry", "containerPort": 5000}],
        "readinessProbe": {
            "httpGet": {"path": "/v2/", "port": 5000},
            "initialDelaySeconds": 1,
            "periodSeconds": 2,
            "timeoutSeconds": 2,
            "failureThreshold": 60,
        },
        "volumeMounts": [{"name": "registry-data", "mountPath": "/var/lib/registry"}],
    }
    registry_pod = {
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        "volumes": [
            {"name": "registry-data", "hostPath": {"path": data_dir, "type": "DirectoryOrCreate"}}
        ],
    }
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": _NAMESPACE},
    }
    registry_documents = [namespace, _deployment("registry", registry_container, registry_pod)]

    pinned_images = []
    for pinned in [agent_image, target_image, *service_images]:
        repository = pinned.split("/", 1)[1].split("@", 1)[0]
        target = next(
            item["target"]
            for item in normalized_archives
            if item["target"].split("/", 1)[1].rsplit(":", 1)[0] == repository
        )
        pinned_images.append({"reference": pinned, "target": target})
    config = {
        "endpoint": endpoint,
        "namespace": _NAMESPACE,
        "registry_manifest": "/etc/ai-agent-stack/registry.yaml",
        "services_manifest": "/etc/ai-agent-stack/eph-bmc-services.yaml",
        "registry_archive": registry_archive,
        "agent_image": agent_image,
        "target_agent_image": target_image,
        "images": normalized_archives,
        "pinned_images": pinned_images,
        "service_names": list(_REQUIRED_SERVICES),
        "wait_seconds": value.get("wait_seconds", 120),
    }
    if not isinstance(config["wait_seconds"], int) or not 1 <= config["wait_seconds"] <= 600:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_WAIT_INVALID")
    return {
        "config": config,
        "registry_documents": registry_documents,
        "service_documents": service_documents,
        **target_registry_config(endpoint, value.get("pause_image") or None),
        "endpoint": endpoint,
        "target_agent_image": target_image,
    }
