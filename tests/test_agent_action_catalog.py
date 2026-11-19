"""Drift guards between the shared orchestration contract and its consumers.

The Cloud Control Agent, the EPH Agent and the Target Agent each enforce an
action allowlist. The shared catalog in ``packages.agent_contracts.orchestration``
is the single source of truth; the control plane mirrors it because its runtime
image does not ship the ``packages`` tree. These tests fail as soon as either
side adds, renames or drops an action without the other.
"""

from uuid import uuid4

import pytest

from apps.cloud_control_agent import api as control_api
from apps.eph_agent import main as eph_main
from apps.target_agent import main as target_main
from packages.agent_contracts import orchestration


def test_control_plane_eph_allowlist_matches_shared_catalog():
    assert control_api.EPH_ACTIONS == orchestration.EPH_ACTIONS


def test_control_plane_target_allowlist_matches_shared_catalog():
    assert control_api.TARGET_ACTIONS == orchestration.TARGET_ACTIONS


def test_eph_agent_capabilities_are_derived_from_shared_catalog():
    assert eph_main.CAPABILITIES == sorted(orchestration.EPH_ACTIONS)


def test_target_agent_capabilities_are_derived_from_shared_catalog():
    # The Target Agent reports its actions in a curated execution order, so the
    # drift guard compares membership (no duplicates, nothing missing, nothing
    # extra) rather than ordering.
    assert len(target_main.CAPABILITIES) == len(set(target_main.CAPABILITIES))
    assert set(target_main.CAPABILITIES) == set(orchestration.TARGET_ACTIONS)


def test_eph_allowlist_contains_bootstrap_render_action():
    assert "eph.bootstrap.render" in orchestration.EPH_ACTIONS


def test_control_plane_checkpoint_phases_follow_lifecycle_order():
    expected = tuple(phase.value for phase in orchestration.PHASE_ORDER)
    assert control_api.CHECKPOINT_PHASES == expected


@pytest.mark.parametrize(
    "phase",
    [phase for phase in orchestration.PHASE_ORDER if not orchestration.is_terminal(phase)],
)
def test_every_dispatch_step_references_catalog_actions(phase):
    plan = orchestration.plan_next_step(
        uuid4(),
        phase,
        {"cluster_name": "cluster-01"},
    )

    assert plan is not None
    assert plan.role == orchestration.role_for_action(plan.action)
    allowed = orchestration.EPH_ACTIONS if plan.role == "eph" else orchestration.TARGET_ACTIONS
    assert plan.action in allowed
    assert plan.next_phase == orchestration.next_phase_after(phase)
