"""EPH single-node bootstrap configuration rendering.

The EPH Agent owns the only bootstrap Kubernetes cluster used to drive the
Metal3 / CAPI provisioning flow. This module renders the configuration that
brings that single node up:

* a ``kubeadm init`` configuration (InitConfiguration + ClusterConfiguration),
* a NoCloud ``cloud-init`` seed that installs the node prerequisites and runs
  ``kubeadm init`` on first boot,
* the EPH Agent Deployment manifest that registers the bootstrap node back to
  the Cloud Control Agent over mTLS.

The renderer is pure: it validates the supplied spec and returns typed
manifests. It never invokes ``kubeadm``, ``containerd`` or ``kubectl``; command
execution stays in the runner injected by the EPH Agent main loop. Missing
optional fields are omitted instead of being filled with hard-coded defaults.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

from .metal3_skill import Metal3SkillError

_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
)


def _valid_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value) or len(value) > 63:
        raise Metal3SkillError(code)
    return value


def _valid_version(value: Any) -> str:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise Metal3SkillError("EPH_BOOTSTRAP_K8S_VERSION_INVALID")
    return value


def _valid_ip(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise Metal3SkillError(code)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise Metal3SkillError(code) from None
    return value


def _valid_cidr(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise Metal3SkillError(code)
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise Metal3SkillError(code) from None
    return str(network)


def _valid_endpoint(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise Metal3SkillError(code)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not _HOSTNAME_PATTERN.fullmatch(value):
            raise Metal3SkillError(code) from None
    return value


def _valid_port(value: Any, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise Metal3SkillError("EPH_BOOTSTRAP_PORT_INVALID")
    return value


def _valid_url(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.startswith(("http://", "https://", "oci://")):
        raise Metal3SkillError(code)
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password:
        raise Metal3SkillError(code)
    return value


def _valid_ntp_servers(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise Metal3SkillError("EPH_BOOTSTRAP_NTP_SERVERS_INVALID")
    normalized: list[str] = []
    for server in value:
        if not isinstance(server, str) or len(server) > 253:
            raise Metal3SkillError("EPH_BOOTSTRAP_NTP_SERVERS_INVALID")
        try:
            ipaddress.ip_address(server)
        except ValueError:
            if not _HOSTNAME_PATTERN.fullmatch(server):
                raise Metal3SkillError("EPH_BOOTSTRAP_NTP_SERVERS_INVALID") from None
        normalized.append(server)
    return normalized


def _validate_agent_block(agent: Any) -> dict[str, Any]:
    if not isinstance(agent, dict):
        raise Metal3SkillError("EPH_BOOTSTRAP_AGENT_BLOCK_REQUIRED")
    control_endpoint = _valid_url(
        agent.get("control_endpoint"),
        "EPH_BOOTSTRAP_AGENT_ENDPOINT_INVALID",
    )
    image = _valid_url(agent.get("image"), "EPH_BOOTSTRAP_AGENT_IMAGE_INVALID")
    mtls_secret_ref = _valid_name(
        agent.get("mtls_secret_ref"),
        "EPH_BOOTSTRAP_AGENT_MTLS_SECRET_REF_INVALID",
    )
    poll_seconds = agent.get("poll_seconds", 15)
    if (
        not isinstance(poll_seconds, int)
        or isinstance(poll_seconds, bool)
        or not 1 <= poll_seconds <= 3600
    ):
        raise Metal3SkillError("EPH_BOOTSTRAP_AGENT_POLL_SECONDS_INVALID")
    agent_id = agent.get("agent_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise Metal3SkillError("EPH_BOOTSTRAP_AGENT_ID_INVALID")
    return {
        "control_endpoint": control_endpoint,
        "image": image,
        "mtls_secret_ref": mtls_secret_ref,
        "poll_seconds": poll_seconds,
        **({"agent_id": agent_id} if agent_id else {}),
    }


def _normalize_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise Metal3SkillError("EPH_BOOTSTRAP_SPEC_INVALID")
    node_name = _valid_name(spec.get("node_name"), "EPH_BOOTSTRAP_NODE_NAME_INVALID")
    k8s_version = _valid_version(spec.get("kubernetes_version"))
    node_ip = _valid_ip(spec.get("node_ip"), "EPH_BOOTSTRAP_NODE_IP_INVALID")
    pod_cidr = _valid_cidr(spec.get("pod_cidr"), "EPH_BOOTSTRAP_POD_CIDR_INVALID")
    service_cidr = _valid_cidr(spec.get("service_cidr"), "EPH_BOOTSTRAP_SERVICE_CIDR_INVALID")
    endpoint = _valid_endpoint(
        spec.get("control_plane_endpoint", node_ip),
        "EPH_BOOTSTRAP_ENDPOINT_INVALID",
    )
    port = _valid_port(spec.get("control_plane_port"), 6443)
    ntp_servers = _valid_ntp_servers(spec.get("ntp_servers"))
    agent = _validate_agent_block(spec.get("agent"))
    return {
        "node_name": node_name,
        "kubernetes_version": k8s_version,
        "node_ip": node_ip,
        "pod_cidr": pod_cidr,
        "service_cidr": service_cidr,
        "control_plane_endpoint": endpoint,
        "control_plane_port": port,
        "ntp_servers": ntp_servers,
        "agent": agent,
    }


def render_kubeadm_init(spec: Any) -> list[dict[str, Any]]:
    """Render the kubeadm init configuration documents for the bootstrap node."""

    normalized = _normalize_spec(spec)
    init_config: dict[str, Any] = {
        "apiVersion": "kubeadm.k8s.io/v1beta4",
        "kind": "InitConfiguration",
        "localAPIEndpoint": {
            "advertiseAddress": normalized["node_ip"],
            "bindPort": normalized["control_plane_port"],
        },
        "nodeRegistration": {
            "name": normalized["node_name"],
            "criSocket": "unix:///run/containerd/containerd.sock",
            "imagePullPolicy": "IfNotPresent",
        },
    }
    endpoint = f"{normalized['control_plane_endpoint']}:{normalized['control_plane_port']}"
    cluster_config: dict[str, Any] = {
        "apiVersion": "kubeadm.k8s.io/v1beta4",
        "kind": "ClusterConfiguration",
        "kubernetesVersion": normalized["kubernetes_version"],
        "controlPlaneEndpoint": endpoint,
        "networking": {
            "dnsDomain": "cluster.local",
            "serviceSubnet": normalized["service_cidr"],
            "podSubnet": normalized["pod_cidr"],
        },
    }
    return [init_config, cluster_config]


def render_cloud_init_seed(spec: Any) -> dict[str, Any]:
    """Render the NoCloud seed (meta-data + user-data) for first-boot bring-up."""

    normalized = _normalize_spec(spec)
    kubeadm_documents = render_kubeadm_init(normalized)
    user_data: dict[str, Any] = {
        "#cloud-config": True,
        "packages": ["containerd", "kubelet", "kubeadm", "kubectl"],
        "write_files": [
            {
                "path": "/etc/kubernetes/kubeadm-init.yaml",
                "content": _serialize_kubeadm_documents(kubeadm_documents),
                "owner": "root:root",
                "permissions": "0644",
            }
        ],
        "runcmd": [
            "kubeadm init --config /etc/kubernetes/kubeadm-init.yaml --upload-certs",
            "mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config",
        ],
    }
    if normalized["ntp_servers"]:
        user_data["ntp"] = {"servers": normalized["ntp_servers"]}
    return {
        "meta-data": {
            "instance-id": normalized["node_name"],
            "local-hostname": normalized["node_name"],
        },
        "user-data": user_data,
    }


def _serialize_kubeadm_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(documents, sort_keys=False)


def render_agent_deployment(spec: Any) -> dict[str, Any]:
    """Render the EPH Agent Deployment that joins the bootstrap node to the plane."""

    normalized = _normalize_spec(spec)
    agent = normalized["agent"]
    namespace = "metal3"
    env: list[dict[str, str]] = [
        {"name": "CLOUD_CONTROL_ENDPOINT", "value": agent["control_endpoint"]},
        {"name": "AGENT_ROLE", "value": "eph"},
        {"name": "POLL_SECONDS", "value": str(agent["poll_seconds"])},
    ]
    if agent.get("agent_id"):
        env.append({"name": "AGENT_ID", "value": agent["agent_id"]})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "eph-agent",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": "eph-agent", "agent-role": "eph"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": "eph-agent"}},
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "eph-agent",
                        "agent-role": "eph",
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "name": "eph-agent",
                            "image": agent["image"],
                            "env": env,
                            "volumeMounts": [
                                {"name": "mtls", "mountPath": "/etc/agent/mtls", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "mtls",
                            "secret": {"secretName": agent["mtls_secret_ref"]},
                        }
                    ],
                },
            },
        },
    }
