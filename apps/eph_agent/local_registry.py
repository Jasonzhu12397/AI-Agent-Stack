"""Render the EPH host-local OCI registry and its supporting services.

All container images used by the EPH and Target Agents are imported from the
Live ISO, tagged, and pushed to the registry on the EPH provisioning NIC. No
external registry is contacted. The vbmc and sushy-tools services run beside
that registry on the EPH host, not as virtual machines.
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
_REQUIRED_SERVICES = ("vbmc", "sushy-tools")
_SERVICE_FIELDS = {
    "image",
    "command",
    "ports",
    "environment",
    "volumes",
    "restart",
    "depends_on",
    "network_mode",
    "privileged",
    "cap_add",
    "devices",
}


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


def target_registry_config(endpoint: Any) -> dict[str, str]:
    if not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
    if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
    return {
        "endpoint": endpoint,
        "containerd_config": (
            "version = 2\n\n"
            '[plugins."io.containerd.grpc.v1.cri".registry]\n'
            '  config_path = "/etc/containerd/certs.d"\n'
        ),
        "hosts_toml": (
            f'server = "http://{endpoint}"\n\n'
            f'[host."http://{endpoint}"]\n'
            '  capabilities = ["pull", "resolve", "push"]\n'
            "  skip_verify = true\n"
        ),
    }


def render_local_registry(
    value: Any,
    *,
    node_ip: str,
    agent_image: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_REQUIRED")
    endpoint = value.get("endpoint", f"{node_ip}:5000")
    if not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
    if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_ENDPOINT_INVALID")
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
    compose_services: dict[str, Any] = {
        "registry": {
            "image": registry_reference,
            "pull_policy": "never",
            "restart": "unless-stopped",
            "ports": [f"{endpoint.rsplit(':', 1)[1]}:5000"],
            "volumes": [f"{data_dir}:/var/lib/registry"],
        }
    }
    required_repositories = {agent_repository, target_repository}
    for name in _REQUIRED_SERVICES:
        service = services[name]
        if not isinstance(service, dict) or set(service) - _SERVICE_FIELDS:
            raise Metal3SkillError("EPH_LOCAL_BMC_SERVICE_INVALID")
        service = dict(service)
        service_endpoint, repository = _pinned_image(
            service.get("image"), "EPH_LOCAL_BMC_SERVICE_IMAGE_INVALID"
        )
        if service_endpoint != endpoint:
            raise Metal3SkillError("EPH_LOCAL_BMC_SERVICE_IMAGE_INVALID")
        required_repositories.add(repository)
        service["pull_policy"] = "never"
        service["restart"] = service.get("restart", "unless-stopped")
        service["depends_on"] = ["registry"]
        compose_services[name] = service
    if not required_repositories.issubset(repositories):
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_IMAGE_SET_INCOMPLETE")

    port = endpoint.rsplit(":", 1)[1]
    service_images = [compose_services[name]["image"] for name in _REQUIRED_SERVICES]
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
        "compose_file": "/etc/ai-agent-stack/docker-compose.yaml",
        "registry_archive": registry_archive,
        "agent_image": agent_image,
        "target_agent_image": target_image,
        "images": normalized_archives,
        "pinned_images": pinned_images,
        "service_images": service_images,
        "wait_seconds": value.get("wait_seconds", 120),
    }
    if not isinstance(config["wait_seconds"], int) or not 1 <= config["wait_seconds"] <= 600:
        raise Metal3SkillError("EPH_LOCAL_REGISTRY_WAIT_INVALID")
    hosts = (
        f'server = "http://{endpoint}"\n\n'
        f'[host."http://{endpoint}"]\n'
        '  capabilities = ["pull", "resolve", "push"]\n'
        "  skip_verify = true\n"
    )
    return {
        "config": config,
        "compose": {"name": "ai-agent-eph", "services": compose_services},
        "hosts_toml": hosts,
        "containerd_config": (
            "version = 2\n\n"
            '[plugins."io.containerd.grpc.v1.cri".registry]\n'
            '  config_path = "/etc/containerd/certs.d"\n'
        ),
        "endpoint": endpoint,
        "port": port,
        "target_agent_image": target_image,
    }
