"""Single source of truth for the actions an Agent may be asked to execute.

The platform used to keep its own ``EPH_COMMANDS`` / ``TARGET_COMMANDS`` string
sets next to the orchestration sets in this package. They drifted: a new
orchestration action could be dispatched only if someone remembered to add it
in the second place too, and an action could be authorized for a node whose
agent never implemented it. Both lists now live here, the orchestration sets
are asserted to be subsets at import time, and the platform imports these
instead of restating them.
"""

from __future__ import annotations

from typing import Final

from packages.agent_contracts.orchestration import EPH_ACTIONS, TARGET_ACTIONS

#: Direct commands the EPH Agent can receive outside the deployment lifecycle.
EPH_DIRECT_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "host.network.observe",
        "node.health",
        "host.preflight",
        "cluster.plan",
        "cluster.health",
        "cluster.network.inspect",
        "cluster.observe",
        "cluster.diagnose",
        "cluster.postflight",
        "k8s.resource.read",
        "capi.cluster.observe",
        "metal3.host.inspect",
        "cluster.manifest.validate",
    }
)

#: Direct commands the Target Agent can receive outside the deployment lifecycle.
TARGET_DIRECT_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "host.network.observe",
        "node.health",
        "host.preflight",
        "cluster.health",
        "cluster.network.inspect",
        "cluster.observe",
        "cluster.diagnose",
        "cluster.postflight",
        "cluster.manifest.validate",
        "cluster.kubernetes.version.upgrade",
        "k8s.resource.read",
    }
)

EPH_AGENT_COMMANDS: Final[frozenset[str]] = EPH_DIRECT_COMMANDS | EPH_ACTIONS
TARGET_AGENT_COMMANDS: Final[frozenset[str]] = TARGET_DIRECT_COMMANDS | TARGET_ACTIONS

COMMANDS_BY_ROLE: Final[dict[str, frozenset[str]]] = {
    "eph": EPH_AGENT_COMMANDS,
    "target": TARGET_AGENT_COMMANDS,
}

#: An orchestration step that is not dispatchable would fail only at runtime,
#: so the invariant is asserted while this module is imported.
assert EPH_ACTIONS <= EPH_AGENT_COMMANDS
assert TARGET_ACTIONS <= TARGET_AGENT_COMMANDS


def commands_for_role(role: str) -> frozenset[str]:
    """Return the authorized command surface for an Agent role."""

    try:
        return COMMANDS_BY_ROLE[role]
    except KeyError as exc:
        raise KeyError(f"unknown agent role {role!r}") from exc


__all__ = [
    "COMMANDS_BY_ROLE",
    "EPH_AGENT_COMMANDS",
    "EPH_DIRECT_COMMANDS",
    "TARGET_AGENT_COMMANDS",
    "TARGET_DIRECT_COMMANDS",
    "commands_for_role",
]
