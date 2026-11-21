"""The authorized Agent command surface must be one list, not two.

The platform previously restated the EPH/Target command sets next to the
orchestration sets. A new step could be authorized in one place only, and an
Agent could be sent an action it never implemented. These tests pin the single
registry and its relationship to the actual handlers.
"""

import re
from pathlib import Path

from packages.agent_contracts.agent_commands import (
    COMMANDS_BY_ROLE,
    EPH_AGENT_COMMANDS,
    TARGET_AGENT_COMMANDS,
    commands_for_role,
)
from packages.agent_contracts.orchestration import EPH_ACTIONS, TARGET_ACTIONS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _handled_actions(agent: str) -> set[str]:
    source = (REPO_ROOT / f"apps/{agent}/main.py").read_text(encoding="utf-8")
    handled = set(re.findall(r'if action == "([a-z0-9._]+)":', source))
    for group in re.findall(r"if action in \(([^)]*)\)", source):
        # Some handlers intentionally share one branch for several actions.
        handled.update(re.findall(r'"([a-z0-9._]+)"', group))
    return handled


def test_orchestration_steps_are_dispatchable():
    assert EPH_ACTIONS <= EPH_AGENT_COMMANDS
    assert TARGET_ACTIONS <= TARGET_AGENT_COMMANDS


def test_platform_uses_the_contract_instead_of_its_own_lists():
    from core import agent_node_views

    assert agent_node_views.EPH_COMMANDS == EPH_AGENT_COMMANDS
    assert agent_node_views.TARGET_COMMANDS == TARGET_AGENT_COMMANDS


def test_every_agent_handler_is_authorized():
    assert _handled_actions("eph_agent") <= EPH_AGENT_COMMANDS
    assert _handled_actions("target_agent") <= TARGET_AGENT_COMMANDS


def test_every_orchestration_step_has_a_handler():
    assert EPH_ACTIONS <= _handled_actions("eph_agent")
    assert TARGET_ACTIONS <= _handled_actions("target_agent")


def test_unknown_roles_fail_closed():
    assert commands_for_role("eph") == EPH_AGENT_COMMANDS
    try:
        commands_for_role("control-plane")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown agent roles must not resolve to a command set")
    assert set(COMMANDS_BY_ROLE) == {"eph", "target"}
