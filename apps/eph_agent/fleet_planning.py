"""Reason about a discovered fleet and decide which machine plays which role.

The lab used to hand a flat list of hosts to the shell. That is not enough: a
target cluster needs a control plane of one, three, five or seven machines and a
data plane of everything else, and the decision has to be explainable because a
wrong master costs an entire deployment.

This module is the reasoning step. It takes the machines the platform
discovered, scores them from reported facts only (CPU, RAM, disk, identity
status), and produces a :class:`FleetPlan` where every assignment carries the
sentence that justified it. Nothing here talks to a cluster; rendering and
applying stay with :mod:`apps.eph_agent.capi_renderer` and
:mod:`apps.eph_agent.metal3_skill`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from packages.agent_contracts.bmc_network import (
    bmc_network_findings,
    emulated_bmc_ports,
)
from packages.agent_contracts.infra_networks import (
    InfraNetworkError,
    plan_infra_networks,
)

from .data_template import (
    DataTemplateError,
    default_data_template,
    profile_data_template,
)

ROLE_LABEL = "ai-agent-stack.io/role"
POOL_LABEL = "ai-agent-stack.io/pool"
CONTROL_PLANE_POOL = "control-plane"
DEFAULT_WORKER_POOL = "worker"
VALID_CONTROL_PLANE_COUNTS = (1, 3, 5, 7)
ROLE_CONTROL_PLANE = "control-plane"
ROLE_WORKER = "worker"


class PlanningError(ValueError):
    """Raised when the requested plan cannot be produced from the given fleet."""


@dataclass(frozen=True)
class MachineSpec:
    """What the platform knows about one machine, without any credential."""

    name: str
    machine_id: str
    project_id: str
    bmc_address: str
    boot_mac: str
    secret_ref: str
    cpu_cores: int = 0
    ram_mib: int = 0
    disk_gib: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    identity_status: str = "unknown"
    online: bool = True
    tls_verify: bool = True

    @property
    def ram_gib(self) -> int:
        return self.ram_mib // 1024


@dataclass(frozen=True)
class Assignment:
    machine: MachineSpec
    role: str
    pool_name: str
    score: int
    reason: str


@dataclass(frozen=True)
class FleetPlan:
    cluster_name: str
    namespace: str
    control_plane_count: int
    assignments: tuple[Assignment, ...]
    warnings: tuple[str, ...] = ()

    def by_role(self, role: str) -> tuple[Assignment, ...]:
        return tuple(item for item in self.assignments if item.role == role)

    @property
    def worker_pools(self) -> dict[str, list[Assignment]]:
        pools: dict[str, list[Assignment]] = {}
        for assignment in self.by_role(ROLE_WORKER):
            pools.setdefault(assignment.pool_name, []).append(assignment)
        return pools

    def rationale(self) -> list[str]:
        return [f"{item.machine.name}: {item.reason}" for item in self.assignments]

    def missing_facts(self, assignment: Assignment) -> list[str]:
        """Facts the plan had to do without, so an operator can enrich them."""

        gaps: list[str] = []
        machine = assignment.machine
        if machine.cpu_cores <= 0:
            gaps.append("cpu_cores not reported")
        if machine.ram_mib <= 0:
            gaps.append("ram_mib not reported")
        if machine.disk_gib <= 0:
            gaps.append("disk_gib not reported")
        if machine.identity_status not in {"verified", "candidate"}:
            gaps.append(f"identity_status is {machine.identity_status}")
        return gaps


_MACHINE_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_IDENTITY_STATUSES = frozenset({"unknown", "unverified", "verified", "candidate", "conflict"})


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningError(f"fleet machine {field_name} is required")
    return value


def machine_spec_from_mapping(value: Any) -> MachineSpec:
    """Validate one platform fleet record before it can influence a role."""

    if not isinstance(value, dict):
        raise PlanningError("fleet machine must be an object")
    name = _required_string(value.get("name"), "name")
    if len(name) > 63 or _MACHINE_NAME.fullmatch(name) is None:
        raise PlanningError("fleet machine name is not a Kubernetes DNS label")
    labels = value.get("labels", {})
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in labels.items()
    ):
        raise PlanningError(f"{name}: labels must be a string map")
    identity_status = value.get("identity_status", "unknown")
    if identity_status not in _IDENTITY_STATUSES:
        raise PlanningError(f"{name}: identity_status is invalid")
    integers = {}
    for field_name in ("cpu_cores", "ram_mib", "disk_gib"):
        item = value.get(field_name, 0)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise PlanningError(f"{name}: {field_name} must be a non-negative integer")
        integers[field_name] = item
    for field_name in ("online", "tls_verify"):
        item = value.get(field_name, True)
        if not isinstance(item, bool):
            raise PlanningError(f"{name}: {field_name} must be a boolean")
        integers[field_name] = item
    return MachineSpec(
        name=name,
        machine_id=_required_string(value.get("machine_id"), "machine_id"),
        project_id=_required_string(value.get("project_id"), "project_id"),
        bmc_address=_required_string(value.get("bmc_address"), "bmc_address"),
        boot_mac=_required_string(value.get("boot_mac"), "boot_mac"),
        secret_ref=_required_string(value.get("secret_ref"), "secret_ref"),
        cpu_cores=integers["cpu_cores"],
        ram_mib=integers["ram_mib"],
        disk_gib=integers["disk_gib"],
        labels=labels,
        identity_status=identity_status,
        online=integers["online"],
        tls_verify=integers["tls_verify"],
    )


def plan_fleet_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a deployment payload's fleet into roles and downstream artifacts.

    The result is deliberately JSON-only: Cloud Control persists the allow-listed
    planning fields across checkpoints, so the BMH and CAPI phases never have to
    re-infer a role from a truncated host list.
    """

    if not isinstance(payload, dict):
        raise PlanningError("fleet planning payload must be an object")
    fleet = payload.get("fleet")
    if not isinstance(fleet, list) or not fleet:
        raise PlanningError("fleet planning requires a non-empty fleet list")
    cluster_name = _required_string(payload.get("cluster_name"), "cluster_name")
    namespace = payload.get("namespace", "metal3")
    if not isinstance(namespace, str) or not namespace:
        raise PlanningError("namespace must be a non-empty string")
    control_plane_count = payload.get("control_plane_count", 3)
    if (
        not isinstance(control_plane_count, int)
        or isinstance(control_plane_count, bool)
        or control_plane_count not in VALID_CONTROL_PLANE_COUNTS
    ):
        raise PlanningError(
            f"control_plane_count must be one of {VALID_CONTROL_PLANE_COUNTS}"
        )
    worker_count = payload.get("worker_replicas")
    if worker_count is not None and (
        not isinstance(worker_count, int)
        or isinstance(worker_count, bool)
        or worker_count < 0
    ):
        raise PlanningError("worker_replicas must be a non-negative integer")
    plan = plan_fleet(
        [machine_spec_from_mapping(item) for item in fleet],
        cluster_name=cluster_name,
        namespace=namespace,
        control_plane_count=control_plane_count,
        worker_count=worker_count,
    )
    workers = plan.by_role(ROLE_WORKER)
    evidence: dict[str, Any] = {
        "fleet_plan": plan_document(plan),
        "hosts": bmh_host_plan(plan),
        "control_plane_count": plan.control_plane_count,
        "worker_replicas": len(workers),
    }
    profile = payload.get("infra_network_profile")
    if isinstance(profile, dict):
        # One segment carries every BMC, the PXE hand-out and the Live ISO, so
        # the plan reports a BMC that would not be reachable from it.
        try:
            network_plan = plan_infra_networks(profile)
        except InfraNetworkError as exc:
            raise PlanningError(str(exc)) from exc
        registry = payload.get("local_registry")
        services = registry.get("services") if isinstance(registry, dict) else None
        evidence["bmc_network"] = bmc_network_findings(
            payload.get("fleet"),
            network_plan,
            emulated_bmc=payload.get("emulated_bmc") is True,
            node_ip=payload.get("node_ip")
            if isinstance(payload.get("node_ip"), str)
            else None,
            emulated_ports=tuple(emulated_bmc_ports(services)),
        )
    image_url = payload.get("image_url")
    control_plane_endpoint = payload.get("control_plane_endpoint")
    if isinstance(image_url, str) and image_url and isinstance(
        control_plane_endpoint, str
    ) and control_plane_endpoint:
        data_template = payload.get("data_template")
        if not isinstance(data_template, dict):
            # The profile still carries bonds, VLANs and pools, so it renders the
            # same document the platform would; the link-id-only fallback below
            # cannot describe those and refuses instead of guessing.
            profile = payload.get("infra_network_profile")
            network_spec = payload.get("network_spec")
            if isinstance(profile, dict):
                try:
                    data_template = profile_data_template(profile)
                except (DataTemplateError, InfraNetworkError) as exc:
                    raise PlanningError(str(exc)) from exc
            elif isinstance(network_spec, dict):
                try:
                    data_template = default_data_template(network_spec)
                except DataTemplateError as exc:
                    raise PlanningError(str(exc)) from exc
            else:
                data_template = None
        worker_labels = payload.get("worker_node_labels")
        worker_data_template = payload.get("worker_data_template")
        ip_pool_manifests = payload.get("ip_pool_manifests")
        sriov_policies = payload.get("sriov_node_policy_manifests")
        evidence["cluster_spec"] = cluster_spec(
            plan,
            control_plane_endpoint=control_plane_endpoint,
            image_url=image_url,
            k8s_version=payload.get("k8s_version", "v1.37.2"),
            data_template=data_template if isinstance(data_template, dict) else None,
            worker_data_template=(
                worker_data_template
                if isinstance(worker_data_template, dict)
                else None
            ),
            worker_node_labels=(
                worker_labels if isinstance(worker_labels, dict) else None
            ),
            registry_endpoint=(
                payload.get("registry_endpoint")
                if isinstance(payload.get("registry_endpoint"), str)
                else None
            ),
            ip_pool_manifests=(
                ip_pool_manifests
                if isinstance(ip_pool_manifests, list)
                and all(isinstance(item, dict) for item in ip_pool_manifests)
                else None
            ),
            sriov_node_policy_manifests=(
                sriov_policies
                if isinstance(sriov_policies, list)
                and all(isinstance(item, dict) for item in sriov_policies)
                else None
            ),
        )
    return evidence


def machine_score(machine: MachineSpec) -> int:
    """Rank a machine for control-plane duty from reported facts only.

    Weights, on purpose: one CPU core buys three points because a control plane
    runs etcd and the API server, one GiB of RAM buys one point because etcd
    latency tracks memory pressure, and four GiB of disk buy one point because
    disk matters for images but rarely decides the role. A verified hardware
    identity adds five points so an attested machine outranks an anonymous one
    with the same shape.
    """

    score = machine.cpu_cores * 3 + machine.ram_gib + machine.disk_gib // 4
    if machine.identity_status == "verified":
        score += 5
    elif machine.identity_status == "conflict":
        score -= 10
    return score


def _explicit_role(machine: MachineSpec) -> str | None:
    value = machine.labels.get(ROLE_LABEL, "").strip().lower()
    if value in {ROLE_CONTROL_PLANE, "control_plane", "master"}:
        return ROLE_CONTROL_PLANE
    if value in {ROLE_WORKER, "data-plane", "data_plane"}:
        return ROLE_WORKER
    return None


def _closest_valid_count(requested: int, available: int) -> int:
    candidates = [count for count in VALID_CONTROL_PLANE_COUNTS if count <= available]
    if not candidates:
        raise PlanningError(
            f"a control plane needs at least one machine, the fleet has {available}"
        )
    if requested in candidates:
        return requested
    closest = min(candidates, key=lambda count: (abs(count - requested), -count))
    return closest


def plan_fleet(
    machines: Iterable[MachineSpec],
    *,
    cluster_name: str,
    namespace: str = "metal3",
    control_plane_count: int = 3,
    worker_count: int | None = None,
) -> FleetPlan:
    """Decide the role of every machine and explain each decision."""

    if not cluster_name:
        raise PlanningError("cluster_name is required")
    if control_plane_count not in VALID_CONTROL_PLANE_COUNTS:
        raise PlanningError(
            f"control_plane_count must be one of {VALID_CONTROL_PLANE_COUNTS}, "
            f"got {control_plane_count}"
        )
    if worker_count is not None and (
        not isinstance(worker_count, int)
        or isinstance(worker_count, bool)
        or worker_count < 0
    ):
        raise PlanningError("worker_count must be a non-negative integer")

    fleet = list(machines)
    if not fleet:
        raise PlanningError("the fleet is empty")
    names = [machine.name for machine in fleet]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise PlanningError(f"duplicate machine name(s): {', '.join(duplicates)}")

    warnings: list[str] = []
    conflicts = [machine.name for machine in fleet if machine.identity_status == "conflict"]
    if conflicts:
        warnings.append(f"identity conflict, excluded from planning: {', '.join(conflicts)}")
    usable = [machine for machine in fleet if machine.identity_status != "conflict"]
    if not usable:
        raise PlanningError("every machine has an identity conflict")

    labelled_masters = [
        machine for machine in usable if _explicit_role(machine) == ROLE_CONTROL_PLANE
    ]
    labelled_workers = [machine for machine in usable if _explicit_role(machine) == ROLE_WORKER]
    unlabelled = [machine for machine in usable if _explicit_role(machine) is None]

    effective_count = _closest_valid_count(control_plane_count, len(usable))
    if worker_count is not None:
        if len(usable) < control_plane_count:
            raise PlanningError(
                f"fleet has {len(usable)} usable machine(s); the requested "
                f"control plane needs {control_plane_count}"
            )
        effective_count = control_plane_count
    elif effective_count != control_plane_count:
        warnings.append(
            f"requested a control plane of {control_plane_count}, but only "
            f"{len(usable)} machine(s) can serve; planning with {effective_count}"
        )
    if labelled_masters:
        if len(labelled_masters) not in VALID_CONTROL_PLANE_COUNTS:
            raise PlanningError(
                f"{len(labelled_masters)} machine(s) are labelled "
                f"{ROLE_LABEL}={ROLE_CONTROL_PLANE}; a control plane must be "
                f"{VALID_CONTROL_PLANE_COUNTS}"
            )
        if effective_count != len(labelled_masters):
            warnings.append(
                f"control plane follows the platform labels ({len(labelled_masters)}), "
                f"not the requested size"
            )
        effective_count = len(labelled_masters)

    ranked = sorted(unlabelled, key=lambda machine: (-machine_score(machine), machine.name))
    inferred_masters = ranked[: max(effective_count - len(labelled_masters), 0)]
    remaining = ranked[len(inferred_masters) :]

    assignments: list[Assignment] = []
    for machine in labelled_masters:
        assignments.append(
            Assignment(
                machine=machine,
                role=ROLE_CONTROL_PLANE,
                pool_name=CONTROL_PLANE_POOL,
                score=machine_score(machine),
                reason=f"labelled {ROLE_LABEL}={ROLE_CONTROL_PLANE} by the platform",
            )
        )
    for machine in sorted(inferred_masters, key=lambda item: item.name):
        assignments.append(
            Assignment(
                machine=machine,
                role=ROLE_CONTROL_PLANE,
                pool_name=CONTROL_PLANE_POOL,
                score=machine_score(machine),
                reason=(
                    f"ranked into the control plane by reported capacity "
                    f"(score {machine_score(machine)}: {machine.cpu_cores} cores, "
                    f"{machine.ram_gib} GiB RAM, {machine.disk_gib} GiB disk, "
                    f"identity {machine.identity_status})"
                ),
            )
        )
    worker_candidates = sorted(labelled_workers + remaining, key=lambda item: item.name)
    if worker_count is not None:
        if len(labelled_workers) > worker_count:
            raise PlanningError(
                f"{len(labelled_workers)} machine(s) are labelled worker, but only "
                f"{worker_count} worker(s) were requested"
            )
        if len(worker_candidates) < worker_count:
            raise PlanningError(
                f"fleet has {len(worker_candidates)} machine(s) left for workers, "
                f"but {worker_count} were requested"
            )
        selected_workers = worker_candidates[:worker_count]
        spare = worker_candidates[worker_count:]
        if spare:
            warnings.append(
                "excluded from this deployment as spare capacity: "
                + ", ".join(machine.name for machine in spare)
            )
    else:
        selected_workers = worker_candidates
    for machine in selected_workers:
        pool = machine.labels.get(POOL_LABEL, "").strip() or DEFAULT_WORKER_POOL
        reason = (
            f"labelled {ROLE_LABEL}={ROLE_WORKER} by the platform"
            if machine in labelled_workers
            else f"left over after the control plane (score {machine_score(machine)})"
        )
        assignments.append(
            Assignment(
                machine=machine,
                role=ROLE_WORKER,
                pool_name=pool,
                score=machine_score(machine),
                reason=reason,
            )
        )

    if effective_count > 1 and effective_count % 2 == 0:  # pragma: no cover - guarded above
        raise PlanningError("a control plane must be an odd size above one")

    return FleetPlan(
        cluster_name=cluster_name,
        namespace=namespace,
        control_plane_count=effective_count,
        assignments=tuple(assignments),
        warnings=tuple(warnings),
    )


def bmh_host_plan(plan: FleetPlan) -> list[dict[str, Any]]:
    """The host plan :func:`capi_renderer.render_bmh_manifests` expects."""

    hosts: list[dict[str, Any]] = []
    for assignment in plan.assignments:
        machine = assignment.machine
        hosts.append(
            {
                "name": machine.name,
                "node_pool_name": assignment.pool_name,
                "secret_ref": machine.secret_ref,
                "boot_mac_address": machine.boot_mac,
                "bmc_address": machine.bmc_address,
                "online": machine.online,
                # nodeType drives which pool the BMH lands in for the target cluster
                "node_type": "controlplane" if assignment.role == ROLE_CONTROL_PLANE else "worker",
                "disable_certificate_verification": not machine.tls_verify,
                "annotations": {
                    "ai-agent-stack.io/machine-id": machine.machine_id,
                    "ai-agent-stack.io/project-id": machine.project_id,
                    "ai-agent-stack.io/role": assignment.role,
                    "ai-agent-stack.io/pool": assignment.pool_name,
                },
            }
        )
    return hosts


def cluster_spec(
    plan: FleetPlan,
    *,
    control_plane_endpoint: str,
    image_url: str,
    k8s_version: str = "v1.37.2",
    data_template: dict[str, Any] | None = None,
    worker_data_template: dict[str, Any] | None = None,
    worker_node_labels: dict[str, list[str]] | None = None,
    registry_endpoint: str | None = None,
    ip_pool_manifests: list[dict[str, Any]] | None = None,
    sriov_node_policy_manifests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The spec :func:`capi_renderer.render_capi_metal3_manifests` expects."""

    labels_by_pool = worker_node_labels or {}
    pools = [
        {
            "name": pool_name,
            "count": len(members),
            "node_labels": labels_by_pool.get(pool_name, []),
            "data_template": worker_data_template or data_template,
        }
        for pool_name, members in sorted(plan.worker_pools.items())
    ]
    return {
        "name": plan.cluster_name,
        "namespace": plan.namespace,
        "infrastructure_provider": "metal3",
        "control_plane_endpoint": control_plane_endpoint,
        "image_url": image_url,
        "k8s_version": k8s_version,
        "control_plane_count": plan.control_plane_count,
        "worker_pools": pools,
        "control_plane_data_template": data_template,
        **({"registry_endpoint": registry_endpoint} if registry_endpoint else {}),
        **({"ip_pool_manifests": ip_pool_manifests} if ip_pool_manifests else {}),
        **(
            {"sriov_node_policy_manifests": sriov_node_policy_manifests}
            if sriov_node_policy_manifests
            else {}
        ),
    }


def plan_document(plan: FleetPlan) -> dict[str, Any]:
    """A JSON-able record of the reasoning, for review and audit."""

    return {
        "cluster_name": plan.cluster_name,
        "namespace": plan.namespace,
        "control_plane_count": plan.control_plane_count,
        "warnings": list(plan.warnings),
        "assignments": [
            {
                "machine": item.machine.name,
                "machine_id": item.machine.machine_id,
                "role": item.role,
                "pool": item.pool_name,
                "score": item.score,
                "reason": item.reason,
                "missing_facts": plan.missing_facts(item),
            }
            for item in plan.assignments
        ],
        "worker_pools": {
            pool: [item.machine.name for item in members]
            for pool, members in plan.worker_pools.items()
        },
    }
