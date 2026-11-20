"""Judge the management cluster in python instead of parsing kubectl in bash.

The lab used to grep ``kubectl get crds``, jq the deployment status and inspect
host containers through shell pipelines. Every one of those steps re-implemented
in shell, where a missing field silently became an empty string and a passing
check. Here the same evidence is read once, as structured data, and turned into
explicit findings the agent (and a human) can read.

Nothing is invented: a check that cannot be evaluated is reported as a failure
with the reason, never as a pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# The CRDs the EPH live-OS management cluster must carry before a target cluster
# can be rendered at all.
REQUIRED_CRDS: tuple[str, ...] = (
    "clusters.cluster.x-k8s.io",
    "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
    "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
    "machinedeployments.cluster.x-k8s.io",
    "machines.cluster.x-k8s.io",
    "machinesets.cluster.x-k8s.io",
    "baremetalhosts.metal3.io",
)

# (namespace, deployment) for every controller that must be fully rolled out.
PROVIDER_DEPLOYMENTS: tuple[tuple[str, str], ...] = (
    ("capm3-system", "capm3-controller-manager"),
    ("capi-system", "capi-controller-manager"),
    ("capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"),
    ("capi-kubeadm-control-plane-system", "capi-kubeadm-control-plane-controller-manager"),
    ("capm3-system", "ipam-controller-manager"),
    ("baremetal-operator-system", "baremetal-operator-controller-manager"),
    ("ai-agent-system", "registry"),
    ("ai-agent-system", "vbmc"),
    ("ai-agent-system", "sushy-tools"),
    ("ai-agent-system", "httpd-infra"),
)

# (label key, label value, namespace, expected replica set count)
PROVIDER_REPLICASETS: tuple[tuple[str, str, str, int], ...] = (
    ("cluster.x-k8s.io/provider", "infrastructure-metal3", "capm3-system", 1),
    ("cluster.x-k8s.io/provider", "cluster-api", "capi-system", 1),
    ("cluster.x-k8s.io/provider", "bootstrap-kubeadm", "capi-kubeadm-bootstrap-system", 1),
    ("cluster.x-k8s.io/provider", "control-plane-kubeadm", "capi-kubeadm-control-plane-system", 1),
    ("cluster.x-k8s.io/provider", "ipam-metal3", "capm3-system", 1),
)

# All EPH infrastructure services are Pods; only network links remain host-side.
REQUIRED_BRIDGES: tuple[str, ...] = ("provisioning", "external")

Runner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Finding:
    """One judgement, with the evidence that produced it."""

    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        return f"{mark} {self.name}" + (f": {self.detail}" if self.detail else "")


def _json_document(result: dict[str, Any]) -> dict[str, Any] | None:
    if not result.get("available") or result.get("returncode") != 0:
        return None
    try:
        document = json.loads(result.get("stdout") or "")
    except (TypeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _kubectl(runner: Runner, arguments: Sequence[str], kubeconfig: str | None) -> dict[str, Any]:
    prefix = ["--kubeconfig", kubeconfig] if kubeconfig else []
    return runner([*prefix, *arguments], 60)


def check_crds(runner: Runner, kubeconfig: str | None = None) -> list[Finding]:
    document = _json_document(_kubectl(runner, ["get", "crds", "-o", "json"], kubeconfig))
    if document is None:
        return [Finding("management cluster CRD inventory", False, "kubectl returned no JSON")]
    names = {
        item.get("metadata", {}).get("name")
        for item in document.get("items", [])
        if isinstance(item, dict)
    }
    return [
        Finding(f"CRD {name}", name in names, "" if name in names else "not installed")
        for name in REQUIRED_CRDS
    ]


def _replica_finding(label: str, status: dict[str, Any]) -> Finding:
    desired = status.get("replicas")
    ready = status.get("readyReplicas", 0)
    ok = isinstance(desired, int) and desired > 0 and ready == desired
    return Finding(label, ok, f"ready={ready} desired={desired}")


def check_deployments(runner: Runner, kubeconfig: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for namespace, name in PROVIDER_DEPLOYMENTS:
        document = _json_document(
            _kubectl(
                runner,
                ["get", "deployment", name, "--namespace", namespace, "-o", "json"],
                kubeconfig,
            )
        )
        label = f"deployment {namespace}/{name}"
        if document is None:
            findings.append(Finding(label, False, "not found"))
            continue
        findings.append(_replica_finding(label, document.get("status", {})))
    return findings


def check_replicasets(runner: Runner, kubeconfig: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for key, value, namespace, expected in PROVIDER_REPLICASETS:
        document = _json_document(
            _kubectl(
                runner,
                [
                    "get",
                    "replicasets",
                    "-l",
                    f"{key}={value}",
                    "--namespace",
                    namespace,
                    "-o",
                    "json",
                ],
                kubeconfig,
            )
        )
        label = f"provider {value} in {namespace}"
        if document is None:
            findings.append(Finding(label, False, "not found"))
            continue
        items = [item for item in document.get("items", []) if isinstance(item, dict)]
        findings.append(
            Finding(
                f"{label} replica set count",
                len(items) == expected,
                f"found={len(items)} expected={expected}",
            )
        )
        for index, item in enumerate(items):
            findings.append(
                _replica_finding(f"{label} replica set {index}", item.get("status", {}))
            )
    return findings


def check_bridges(host_runner: Runner) -> list[Finding]:
    result = host_runner(["ip", "-j", "link", "show"], 30)
    if not result.get("available") or result.get("returncode") != 0:
        return [Finding("host link inventory", False, "ip link failed")]
    try:
        links = json.loads(result.get("stdout") or "[]")
    except (TypeError, ValueError):
        return [Finding("host link inventory", False, "ip returned no JSON")]
    names = {item.get("ifname") for item in links if isinstance(item, dict)}
    return [
        Finding(
            f"bridge {name}",
            name in names,
            "" if name in names else "missing on the provisioning host",
        )
        for name in REQUIRED_BRIDGES
    ]


def check_ironic_auth(http_probe: Callable[[str], int | None], ironic_url: str) -> list[Finding]:
    """Ironic must refuse anonymous reads; 200 means the API is wide open."""

    endpoint = f"{ironic_url.rstrip('/')}/nodes"
    status = http_probe(endpoint)
    if status is None:
        return [Finding("Ironic API authentication", False, f"no response from {endpoint}")]
    if status == 401:
        return [Finding("Ironic API authentication", True, "401 for anonymous access")]
    if status == 200:
        return [
            Finding(
                "Ironic API authentication",
                False,
                "200 for anonymous access: the endpoint is exposed",
            )
        ]
    return [Finding("Ironic API authentication", False, f"got {status}, expected 401")]


def verify_management_cluster(
    runner: Runner,
    *,
    kubeconfig: str | None = None,
    host_runner: Runner | None = None,
    http_probe: Callable[[str], int | None] | None = None,
    ironic_url: str | None = None,
) -> list[Finding]:
    """Collect every management-cluster judgement in one pass."""

    findings: list[Finding] = []
    findings.extend(check_crds(runner, kubeconfig))
    findings.extend(check_deployments(runner, kubeconfig))
    findings.extend(check_replicasets(runner, kubeconfig))
    if host_runner is not None:
        findings.extend(check_bridges(host_runner))
    if http_probe is not None and ironic_url:
        findings.extend(check_ironic_auth(http_probe, ironic_url))
    return findings
