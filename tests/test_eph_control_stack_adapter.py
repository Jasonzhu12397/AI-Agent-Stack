from __future__ import annotations

import json

import pytest

from apps.eph_agent.control_stack import StackComponent, plan_control_stack
from apps.eph_agent.control_stack_adapter import (
    ControlStackAdapterError,
    apply_control_stack,
    wait_for_control_stack_ready,
)


def _deployment(controller: str, ready: bool) -> dict[str, object]:
    replicas = 1
    available = 1 if ready else 0
    return {
        "metadata": {"name": controller},
        "spec": {"replicas": replicas},
        "status": {"availableReplicas": available, "readyReplicas": available},
    }


def _make_runner(existing: set[str], applied: list[str], ready: bool = True):
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        if arguments[0] == "get":
            controller = arguments[2]
            if controller in existing:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps(_deployment(controller, ready)),
                    "stderr": "",
                }
            return {"available": False, "returncode": 1, "stdout": "", "stderr": "NotFound"}
        if arguments[0] == "apply":
            if "--dry-run=server" not in arguments:
                applied.append(arguments[-1])
            return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}
        return {"available": False, "returncode": 1, "stderr": "unknown"}

    return runner, calls


def test_apply_skips_existing_and_records_pending_without_manifest():
    plan = plan_control_stack({})
    existing = {"cert-manager-controller"}
    applied: list[str] = []
    runner, calls = _make_runner(existing, applied)

    evidence = apply_control_stack(plan, runner)

    assert evidence["already_present_components"] == ["cert-manager"]
    assert evidence["apply_pending_components"] == [
        "capi-core",
        "baremetal-operator",
        "ironic",
        "capm3",
    ]
    assert evidence["applied_components"] == []
    assert evidence["side_effects"] is False
    assert any(call[0] == "get" for call in calls)
    assert not any(call[0] == "apply" for call in calls)


def test_apply_applies_supplied_manifest_for_missing_controller():
    plan = plan_control_stack({})
    applied: list[str] = []
    manifests = {
        "cert-manager": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "cert-manager-controller", "namespace": "cert-manager"},
        }
    }
    runner, _calls = _make_runner(set(), applied)

    evidence = apply_control_stack(plan, runner, manifests=manifests)

    assert evidence["applied_components"] == ["cert-manager"]
    assert evidence["apply_pending_components"] == [
        "capi-core",
        "baremetal-operator",
        "ironic",
        "capm3",
    ]
    assert evidence["side_effects"] is True
    assert len(applied) == 1


def test_wait_ready_succeeds_when_all_controllers_are_ready():
    plan = plan_control_stack({})
    existing = {component.controller for component in plan}
    applied: list[str] = []
    runner, _ = _make_runner(existing, applied, ready=True)

    evidence = wait_for_control_stack_ready(plan, runner, timeout_seconds=60)

    assert evidence["ready_component_count"] == len(plan)
    assert evidence["side_effects"] is False


def test_wait_ready_times_out_when_controllers_stay_not_ready():
    plan = plan_control_stack({})
    existing = {component.controller for component in plan}
    applied: list[str] = []
    runner, _ = _make_runner(existing, applied, ready=False)
    ticks = {"value": 0}

    def monotonic() -> float:
        ticks["value"] += 100
        return float(ticks["value"])

    with pytest.raises(ControlStackAdapterError, match="CONTROL_STACK_READY_TIMEOUT"):
        wait_for_control_stack_ready(
            plan,
            runner,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
            timeout_seconds=60,
        )


def test_apply_rejects_empty_plan():
    runner, _ = _make_runner(set(), [])
    with pytest.raises(ControlStackAdapterError, match="CONTROL_STACK_PLAN_INVALID"):
        apply_control_stack([], runner)


def test_apply_rejects_non_monotonic_order():
    bad_plan = [
        StackComponent("a", "ns", "c1", 1, None),
        StackComponent("b", "ns", "c2", 1, None),
    ]
    runner, _ = _make_runner(set(), [])
    with pytest.raises(ControlStackAdapterError, match="CONTROL_STACK_PLAN_ORDER_INVALID"):
        apply_control_stack(bad_plan, runner)
