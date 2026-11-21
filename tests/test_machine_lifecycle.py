"""The machine lifecycle contract is the single source of allowed transitions."""

import pytest
from agent_contracts.machine_lifecycle import (
    COMMISSIONED,
    DISCOVERED,
    IDENTITY_REVIEW,
    RETIRED,
    STATES,
    TRANSITIONS,
    allowed_targets,
    normalize_reason,
    transition_allowed,
)


def test_every_state_is_reachable_from_another_state():
    reachable = {target for targets in TRANSITIONS.values() for target in targets}
    assert reachable == set(STATES)


def test_a_discovered_machine_goes_to_identity_review_only():
    assert allowed_targets(DISCOVERED) == (IDENTITY_REVIEW,)


def test_identity_review_can_be_accepted_or_sent_back():
    assert set(allowed_targets(IDENTITY_REVIEW)) == {COMMISSIONED, DISCOVERED}


def test_commissioned_machines_can_only_be_retired():
    assert allowed_targets(COMMISSIONED) == (RETIRED,)


def test_a_retired_machine_can_be_rediscovered():
    assert allowed_targets(RETIRED) == (DISCOVERED,)


@pytest.mark.parametrize("state", [None, "", "unknown", 7])
def test_unknown_states_have_no_transitions(state):
    assert allowed_targets(state) == ()


def test_transition_allowed_requires_both_states():
    assert transition_allowed(DISCOVERED, IDENTITY_REVIEW)
    assert not transition_allowed(DISCOVERED, COMMISSIONED)
    assert not transition_allowed(None, IDENTITY_REVIEW)


def test_reasons_are_trimmed_and_bounded():
    assert normalize_reason("   serial matches intake record  ") == "serial matches intake record"
    assert normalize_reason("short") == ""
    assert normalize_reason("x" * 501) == ""
    assert normalize_reason(None) == ""


def test_retired_machines_may_not_be_booted():
    from agent_contracts.machine_lifecycle import action_blocked

    assert action_blocked(RETIRED, "pxe_boot") is True
    assert action_blocked(RETIRED, "discovery") is True


def test_active_machines_may_be_booted():
    from agent_contracts.machine_lifecycle import action_blocked

    for state in (DISCOVERED, IDENTITY_REVIEW, COMMISSIONED):
        assert action_blocked(state, "pxe_boot") is False
        assert action_blocked(state, "discovery") is False


def test_unknown_states_fail_closed_for_boot_actions():
    from agent_contracts.machine_lifecycle import action_blocked

    assert action_blocked(None, "pxe_boot") is True
    assert action_blocked("weird", "discovery") is True


def test_only_boot_actions_are_gated():
    from agent_contracts.machine_lifecycle import action_blocked

    with pytest.raises(ValueError):
        action_blocked(DISCOVERED, "firmware")
