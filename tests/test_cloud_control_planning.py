"""The cloud control plane plans the next step; callers do not pick actions.

These tests lock the control-plane side of the shared orchestration contract:
the dispatch decision is derived from the deployment checkpoint phase, so the
platform API or an operator can never skip a lifecycle step by choosing an
arbitrary role/action pair.
"""

from uuid import uuid4

import pytest

from apps.cloud_control_agent.api import (
    EPH_ACTIONS,
    TARGET_ACTIONS,
    ControlApiError,
    plan_deployment_command,
)
from packages.agent_contracts import orchestration


def test_plan_deployment_command_starts_with_eph_preflight():
    plan = plan_deployment_command(uuid4(), "not_started", {"cluster_name": "edge-01"})
    assert plan["terminal"] is False
    step = plan["next_step"]
    assert step["role"] == "eph"
    assert step["action"] == "eph.preflight"
    assert step["next_phase"] == "eph_single_node_ready"
    assert step["payload"]["cluster_name"] == "edge-01"


def test_plan_deployment_command_applies_the_control_stack_before_bmh_registration():
    step = plan_deployment_command(uuid4(), "eph_single_node_ready", {})["next_step"]
    assert step["action"] == "eph.metal3.bmh.apply"
    assert step["next_phase"] == "bmh_registered"


def test_plan_deployment_command_hands_over_to_target_after_control_plane_ready():
    step = plan_deployment_command(uuid4(), "bmh_ready", {})["next_step"]
    assert step["role"] == "eph"
    assert step["action"] == "eph.capi.cluster.apply"
    assert step["next_phase"] == "target_control_plane_ready"

    step = plan_deployment_command(uuid4(), "target_control_plane_ready", {})["next_step"]
    assert step["role"] == "target"
    assert step["action"] == "target.handoff.evaluate"


def test_plan_deployment_command_reports_terminal_deployment():
    plan = plan_deployment_command(uuid4(), "completed", {})
    assert plan["terminal"] is True
    assert plan["next_step"] is None


def test_plan_deployment_command_idempotency_key_is_stable_per_phase():
    deployment_id = uuid4()
    first = plan_deployment_command(deployment_id, "bmh_registered", {})["next_step"]
    second = plan_deployment_command(deployment_id, "bmh_registered", {})["next_step"]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["action"] == "eph.metal3.bmh.wait_ready"


def test_plan_deployment_command_rejects_unknown_phase():
    with pytest.raises(ControlApiError):
        plan_deployment_command(uuid4(), "phase-does-not-exist", {})


def test_plan_deployment_command_rejects_non_mapping_spec():
    with pytest.raises(ControlApiError):
        plan_deployment_command(uuid4(), "not_started", ["bad-spec"])


def test_plan_deployment_command_covers_every_lifecycle_phase():
    deployment_id = uuid4()
    for phase in orchestration.PHASE_ORDER:
        plan = plan_deployment_command(deployment_id, phase.value, {})
        if orchestration.is_terminal(phase):
            assert plan["terminal"] is True
            continue
        step = plan["next_step"]
        allowed = EPH_ACTIONS if step["role"] == "eph" else TARGET_ACTIONS
        assert step["action"] in allowed
        assert step["next_phase"] == orchestration.next_phase_after(phase).value
