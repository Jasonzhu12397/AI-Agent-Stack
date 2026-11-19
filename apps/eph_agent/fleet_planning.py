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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

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
) -> FleetPlan:
    """Decide the role of every machine and explain each decision."""

    if not cluster_name:
        raise PlanningError("cluster_name is required")
    if control_plane_count not in VALID_CONTROL_PLANE_COUNTS:
        raise PlanningError(
            f"control_plane_count must be one of {VALID_CONTROL_PLANE_COUNTS}, "
            f"got {control_plane_count}"
        )

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
    if effective_count != control_plane_count:
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
    for machine in sorted(labelled_workers + remaining, key=lambda item: item.name):
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
    worker_node_labels: dict[str, list[str]] | None = None,
    registry_endpoint: str | None = None,
) -> dict[str, Any]:
    """The spec :func:`capi_renderer.render_capi_metal3_manifests` expects."""

    labels_by_pool = worker_node_labels or {}
    pools = [
        {
            "name": pool_name,
            "count": len(members),
            "node_labels": labels_by_pool.get(pool_name, []),
            "data_template": data_template,
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
