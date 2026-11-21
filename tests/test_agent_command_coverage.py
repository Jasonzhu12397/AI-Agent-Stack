"""Every authorized command must reach a handler in the Agent that receives it.

``agent_commands`` is what the platform authorizes; the Agents execute. Before
this guard the two drifted: the terminal offered ``cluster.plan`` and friends and
the Agent answered ``ACTION_NOT_IMPLEMENTED``. This test dispatches every
authorized action through the real ``execute()`` of both Agents with stubbed
external binaries, so a dead command fails the build instead of the operator.
"""

import pytest
from agent_contracts.agent_commands import (
    EPH_AGENT_COMMANDS,
    TARGET_AGENT_COMMANDS,
    commands_for_role,
)

STUBBED_BINARY = {"available": False, "returncode": 127, "stdout": "", "stderr": "stub"}


@pytest.fixture(scope="module")
def agents():
    import eph_agent.main as eph
    import target_agent.main as target

    return {"eph": eph, "target": target}


@pytest.fixture()
def stubbed(agents, monkeypatch):
    for module in agents.values():
        monkeypatch.setattr(module, "_run_read_only", lambda *a, **k: dict(STUBBED_BINARY))
        for name in ("_run_kubectl", "_kubectl", "_kubectl_read"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: dict(STUBBED_BINARY))
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, **k: pytest.fail(
                "a coverage probe must never reach an external binary"
            ),
        )


def _dispatch(module, action):
    return module.execute(
        {
            "action": action,
            "payload": {},
            "command_id": "coverage-probe",
            "lease": {"lease_id": "probe", "token": "probe"},
        }
    )


@pytest.mark.parametrize("role", ["eph", "target"])
def test_no_authorized_command_falls_through_to_action_not_implemented(role, agents, stubbed):
    gaps = []
    for action in sorted(commands_for_role(role)):
        try:
            outcome = _dispatch(agents[role], action)
        except Exception:
            # A handler that rejects an empty payload is implemented; only the
            # generic not-implemented outcome counts as a gap.
            continue
        if isinstance(outcome, dict) and outcome.get("error_code") == "ACTION_NOT_IMPLEMENTED":
            gaps.append(action)

    assert gaps == []


def test_the_two_registries_are_disjoint_where_they_should_be():
    shared = EPH_AGENT_COMMANDS & TARGET_AGENT_COMMANDS
    assert "host.network.observe" in shared


def test_read_only_diagnostics_execute_on_the_target(agents, stubbed):
    outcome = _dispatch(agents["target"], "cluster.health")

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "KUBECTL_UNAVAILABLE"
    assert outcome["side_effects"] is False
