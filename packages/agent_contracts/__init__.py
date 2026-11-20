"""Versioned, side-effect-free contracts for the three Agent roles.

Exports are resolved lazily. Importing a pure submodule (for example
``packages.agent_contracts.ironic_spec``) must not drag in pydantic models,
because lightweight jobs such as the Metal3 CRD integration only install
PyYAML.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "AgentHeartbeat": "models",
    "AgentIdentity": "models",
    "AgentRole": "models",
    "CommandEnvelope": "models",
    "CommandLease": "models",
    "CommandResult": "models",
    "DeploymentCheckpoint": "models",
    "DeploymentPhase": "orchestration",
    "OrchestrationError": "orchestration",
    "StepPlan": "orchestration",
    "build_idempotency_key": "orchestration",
    "build_payload": "orchestration",
    "is_terminal": "orchestration",
    "next_phase_after": "orchestration",
    "phase_index": "orchestration",
    "plan_next_step": "orchestration",
    "role_for_action": "orchestration",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
