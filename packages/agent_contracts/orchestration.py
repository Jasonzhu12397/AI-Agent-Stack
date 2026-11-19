"""Deployment lifecycle state machine shared by the three Agent roles.

This module is side-effect free: it maps a deployment checkpoint phase to the
next autonomous step (role, action, idempotency key, target phase, payload
skeleton) for the Metal3 / CAPI / Ironic Kubernetes provisioning flow. The
Cloud Control Agent and the platform worker reuse the same mapping so the
orchestration boundary cannot drift between the control plane and the agents.

Responsibility boundary (locked by the architecture inventory):

* ``cloud_control``: receive the deployment request, authorize, budget,
  dispatch commands and aggregate state; it does not approve a target takeover.
* ``eph``: single-node bootstrap, source-side preflight, target control plane
  bootstrap, BMH registration and CAPI cluster rendering on the bootstrap
  cluster.
* ``target``: autonomous takeover after the target control plane is ready,
  CAPI pivot, BMH ownership migration, EPH cleanup, worker provisioning and
  final attestation.

The state machine only plans the next step. It does not talk to a database,
the control plane, or any cluster; persistence and command dispatch live in
the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class OrchestrationError(ValueError):
    """Raised when a phase, role or action cannot be resolved by the planner."""


class DeploymentPhase(StrEnum):
    NOT_STARTED = "not_started"
    EPH_SINGLE_NODE_READY = "eph_single_node_ready"
    CONTROL_STACK_READY = "control_stack_ready"
    BMH_REGISTERED = "bmh_registered"
    BMH_READY = "bmh_ready"
    TARGET_CONTROL_PLANE_READY = "target_control_plane_ready"
    TARGET_AGENT_READY = "target_agent_ready"
    CAPI_PIVOT_VERIFIED = "capi_pivot_verified"
    BMH_REGISTRY_MIGRATED = "bmh_registry_migrated"
    EPH_CLEANUP_VERIFIED = "eph_cleanup_verified"
    WORKER_PROVISIONING = "worker_provisioning"
    WORKERS_READY = "workers_ready"
    COMPLETED = "completed"


#: Ordered lifecycle phases, matching the control-plane checkpoint ordering.
PHASE_ORDER: tuple[DeploymentPhase, ...] = (
    DeploymentPhase.EPH_SINGLE_NODE_READY,
    DeploymentPhase.CONTROL_STACK_READY,
    DeploymentPhase.BMH_REGISTERED,
    DeploymentPhase.BMH_READY,
    DeploymentPhase.TARGET_CONTROL_PLANE_READY,
    DeploymentPhase.TARGET_AGENT_READY,
    DeploymentPhase.CAPI_PIVOT_VERIFIED,
    DeploymentPhase.BMH_REGISTRY_MIGRATED,
    DeploymentPhase.EPH_CLEANUP_VERIFIED,
    DeploymentPhase.WORKER_PROVISIONING,
    DeploymentPhase.WORKERS_READY,
    DeploymentPhase.COMPLETED,
)

#: Actions the EPH Agent is allowed to claim, mirrored from the control plane.
EPH_ACTIONS: frozenset[str] = frozenset(
    {
        "eph.preflight",
        "eph.source.observe",
        "eph.bootstrap.render",
        "eph.controlstack.apply",
        "eph.metal3.bmh.apply",
        "eph.metal3.bmh.observe",
        "eph.metal3.bmh.wait_ready",
        "eph.capi.cluster.apply",
        "eph.capi.cluster.observe",
        "eph.capi.cluster.wait_ready",
    }
)

#: Actions the Target Agent is allowed to claim, mirrored from the control plane.
TARGET_ACTIONS: frozenset[str] = frozenset(
    {
        "target.cluster.observe",
        "target.capi.observe",
        "target.capi.pivot.dry_run",
        "target.capi.pivot",
        "target.metal3.observe",
        "target.metal3.verify_ownership",
        "target.worker.ntp.validate",
        "target.ntp.control_plane.render",
        "target.ntp.control_plane.verify_bootstrap",
        "target.ntp.internal.probe",
        "target.handoff.evaluate",
        "target.eph.cleanup",
        "target.worker.provision",
        "target.network.apply",
    }
)

TERMINAL_PHASES: frozenset[DeploymentPhase] = frozenset(
    {DeploymentPhase.COMPLETED}
)

#: Checkpoint that an EPH action advances to after it succeeds. The EPH Agent
#: imports this single source instead of carrying a second hand-written map.
EPH_CHECKPOINT_PHASES: dict[str, DeploymentPhase] = {
    "eph.preflight": DeploymentPhase.EPH_SINGLE_NODE_READY,
    "eph.controlstack.apply": DeploymentPhase.CONTROL_STACK_READY,
    "eph.metal3.bmh.apply": DeploymentPhase.BMH_REGISTERED,
    "eph.metal3.bmh.wait_ready": DeploymentPhase.BMH_READY,
    "eph.capi.cluster.apply": DeploymentPhase.TARGET_CONTROL_PLANE_READY,
}

#: Checkpoint advanced by deterministic Target actions. ``target.handoff.evaluate``
#: is excluded because its next phase depends on the current lifecycle phase.
TARGET_CHECKPOINT_PHASES: dict[str, DeploymentPhase] = {
    "target.capi.pivot": DeploymentPhase.CAPI_PIVOT_VERIFIED,
    "target.metal3.verify_ownership": DeploymentPhase.BMH_REGISTRY_MIGRATED,
    "target.eph.cleanup": DeploymentPhase.EPH_CLEANUP_VERIFIED,
    "target.worker.provision": DeploymentPhase.WORKER_PROVISIONING,
}


@dataclass(frozen=True)
class StepPlan:
    """The next autonomous step the control plane should dispatch."""

    role: str
    action: str
    next_phase: DeploymentPhase
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)


#: Per-phase dispatch table. Each entry maps the current phase to the action the
#: owning role must run to reach ``next_phase``.
_DISPATCH_TABLE: dict[DeploymentPhase, tuple[str, str, DeploymentPhase]] = {
    DeploymentPhase.NOT_STARTED: (
        "eph",
        "eph.preflight",
        DeploymentPhase.EPH_SINGLE_NODE_READY,
    ),
    DeploymentPhase.EPH_SINGLE_NODE_READY: (
        "eph",
        "eph.controlstack.apply",
        DeploymentPhase.CONTROL_STACK_READY,
    ),
    DeploymentPhase.CONTROL_STACK_READY: (
        "eph",
        "eph.metal3.bmh.apply",
        DeploymentPhase.BMH_REGISTERED,
    ),
    DeploymentPhase.BMH_REGISTERED: (
        "eph",
        "eph.metal3.bmh.wait_ready",
        DeploymentPhase.BMH_READY,
    ),
    DeploymentPhase.BMH_READY: (
        "eph",
        "eph.capi.cluster.apply",
        DeploymentPhase.TARGET_CONTROL_PLANE_READY,
    ),
    DeploymentPhase.TARGET_CONTROL_PLANE_READY: (
        "target",
        "target.handoff.evaluate",
        DeploymentPhase.TARGET_AGENT_READY,
    ),
    DeploymentPhase.TARGET_AGENT_READY: (
        "target",
        "target.capi.pivot",
        DeploymentPhase.CAPI_PIVOT_VERIFIED,
    ),
    DeploymentPhase.CAPI_PIVOT_VERIFIED: (
        "target",
        "target.metal3.verify_ownership",
        DeploymentPhase.BMH_REGISTRY_MIGRATED,
    ),
    DeploymentPhase.BMH_REGISTRY_MIGRATED: (
        "target",
        "target.eph.cleanup",
        DeploymentPhase.EPH_CLEANUP_VERIFIED,
    ),
    DeploymentPhase.EPH_CLEANUP_VERIFIED: (
        "target",
        "target.worker.provision",
        DeploymentPhase.WORKER_PROVISIONING,
    ),
    DeploymentPhase.WORKER_PROVISIONING: (
        "target",
        "target.handoff.evaluate",
        DeploymentPhase.WORKERS_READY,
    ),
    DeploymentPhase.WORKERS_READY: (
        "target",
        "target.handoff.evaluate",
        DeploymentPhase.COMPLETED,
    ),
}


def _normalize_phase(phase: str | DeploymentPhase) -> DeploymentPhase:
    if isinstance(phase, DeploymentPhase):
        return phase
    try:
        return DeploymentPhase(phase)
    except ValueError as exc:
        raise OrchestrationError("DEPLOYMENT_PHASE_INVALID") from exc


def phase_index(phase: str | DeploymentPhase) -> int:
    """Return the zero-based lifecycle index; ``not_started`` is ``-1``."""

    normalized = _normalize_phase(phase)
    if normalized is DeploymentPhase.NOT_STARTED:
        return -1
    return PHASE_ORDER.index(normalized)


def is_terminal(phase: str | DeploymentPhase) -> bool:
    return _normalize_phase(phase) in TERMINAL_PHASES


def role_for_action(action: str) -> str:
    """Resolve the owning role for an action, failing closed on unknown actions."""

    if action in EPH_ACTIONS:
        return "eph"
    if action in TARGET_ACTIONS:
        return "target"
    raise OrchestrationError("ACTION_ROLE_UNRESOLVED")


def _validated_role_action(role: str, action: str) -> None:
    allowed = EPH_ACTIONS if role == "eph" else TARGET_ACTIONS if role == "target" else frozenset()
    if action not in allowed:
        raise OrchestrationError("COMMAND_ACTION_NOT_ALLOWED_FOR_AGENT_ROLE")


def build_idempotency_key(deployment_id: UUID, action: str, next_phase: str) -> str:
    """Stable per-deployment, per-action, per-phase key for command dedup."""

    return f"ai-agent-stack:{deployment_id}:{action}:{next_phase}"


def _scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def build_payload(
    deployment_id: UUID,
    action: str,
    spec: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a payload skeleton from the deployment spec.

    Only typed, scalar or dict values present in the spec are copied; missing
    fields are omitted rather than filled with hard-coded defaults. Callers
    enrich the payload with cluster-specific values after the runner observes
    the live state.
    """

    if not isinstance(spec, dict):
        raise OrchestrationError("DEPLOYMENT_SPEC_INVALID")
    payload: dict[str, Any] = {"deployment_id": str(deployment_id), "action": action}

    scalar_fields = (
        "cluster_name",
        "namespace",
        "kubernetes_version",
        "k8s_version",
        "worker_replicas",
        "control_plane_count",
        "baremetal_host_namespace",
        "machine_deployment_name",
        "image_url",
        "control_plane_endpoint",
        "registry_endpoint",
    )
    for field_name in scalar_fields:
        value = _scalar(spec.get(field_name))
        if value is not None:
            payload[field_name] = value

    worker_config = spec.get("worker_provisioning")
    if isinstance(worker_config, dict):
        payload["worker_provisioning"] = worker_config

    for field_name in ("data_template", "worker_node_labels"):
        value = spec.get(field_name)
        if isinstance(value, dict):
            payload[field_name] = value

    for field_name in ("hosts", "fleet"):
        value = spec.get(field_name)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            payload[field_name] = value

    if isinstance(evidence, dict) and evidence:
        if (
            action == "eph.metal3.bmh.apply"
            and "hosts" not in payload
            and isinstance(evidence.get("hosts"), list)
        ):
            payload["hosts"] = evidence["hosts"]
        if (
            action == "eph.capi.cluster.apply"
            and "cluster_spec" not in payload
            and isinstance(evidence.get("cluster_spec"), dict)
        ):
            payload["cluster_spec"] = evidence["cluster_spec"]
        if (
            action == "target.worker.provision"
            and "worker_replicas" not in payload
            and isinstance(evidence.get("worker_replicas"), int)
            and not isinstance(evidence.get("worker_replicas"), bool)
        ):
            payload["worker_replicas"] = evidence["worker_replicas"]
        payload["evidence"] = evidence

    return payload


def next_phase_for_action(
    current_phase: str | DeploymentPhase,
    action: str,
) -> DeploymentPhase | None:
    """Return the phase a dispatchable action advances to, else ``None``.

    Only the action planned for the current phase is accepted. Observation and
    diagnostic actions therefore never create a phantom checkpoint transition.
    """

    phase = _normalize_phase(current_phase)
    if is_terminal(phase):
        return None
    entry = _DISPATCH_TABLE.get(phase)
    if entry is None or entry[1] != action:
        return None
    return entry[2]


def plan_next_step(
    deployment_id: UUID,
    current_phase: str | DeploymentPhase,
    spec: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> StepPlan | None:
    """Plan the next dispatchable step for the current checkpoint phase.

    Returns ``None`` for a terminal deployment. Raises :class:`OrchestrationError`
    for an unknown phase, a spec that is not a mapping, or a dispatch entry whose
    role/action pair is inconsistent with the action catalog.
    """

    phase = _normalize_phase(current_phase)
    if is_terminal(phase):
        return None

    entry = _DISPATCH_TABLE.get(phase)
    if entry is None:
        raise OrchestrationError("DEPLOYMENT_PHASE_NO_DISPATCH")

    role, action, next_phase = entry
    _validated_role_action(role, action)
    payload = build_payload(deployment_id, action, spec, evidence)
    return StepPlan(
        role=role,
        action=action,
        next_phase=next_phase,
        idempotency_key=build_idempotency_key(deployment_id, action, next_phase),
        payload=payload,
    )


def next_phase_after(phase: str | DeploymentPhase) -> DeploymentPhase | None:
    """Return the phase reached after the planned step, or ``None`` if terminal."""

    plan = plan_next_step(
        deployment_id=UUID("00000000-0000-0000-0000-000000000000"),
        current_phase=phase,
        spec={},
    )
    return plan.next_phase if plan is not None else None
