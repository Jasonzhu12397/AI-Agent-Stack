"""Versioned, side-effect-free contracts for the three Agent roles."""

from .models import (
    AgentHeartbeat,
    AgentIdentity,
    AgentRole,
    CommandEnvelope,
    CommandLease,
    CommandResult,
    DeploymentCheckpoint,
)
from .orchestration import (
    DeploymentPhase,
    OrchestrationError,
    StepPlan,
    build_idempotency_key,
    build_payload,
    is_terminal,
    next_phase_after,
    phase_index,
    plan_next_step,
    role_for_action,
)

__all__ = [
    "AgentHeartbeat",
    "AgentIdentity",
    "AgentRole",
    "CommandEnvelope",
    "CommandLease",
    "CommandResult",
    "DeploymentCheckpoint",
    "DeploymentPhase",
    "OrchestrationError",
    "StepPlan",
    "build_idempotency_key",
    "build_payload",
    "is_terminal",
    "next_phase_after",
    "phase_index",
    "plan_next_step",
    "role_for_action",
]
