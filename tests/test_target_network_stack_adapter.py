from __future__ import annotations

import json

import pytest

from apps.target_agent.network_plugins import plan_network_stack
from apps.target_agent.network_stack_adapter import (
    NetworkStackAdapterError,
    apply_network_stack,
    wait_for_network_stack_ready,
)

NAMESPACE = "metal3"


def _spec() -> dict[str, object]:
    return {
        "plugins": {
            "calico": {
                "config": {"resource_name": "calico-node", "resource_kind": "DaemonSet"}
            },
            "sriov": {
                "enabled": True,
                "config": {
                    "resource_name": "sriov-device-plugin",
                    "resource_kind": "DaemonSet",
                },
            },
        }
    }


def _workload(resource: str, kind: str, ready: bool) -> dict[str, object]:
    if kind.lower() == "daemonset":
        return {
            "metadata": {"name": resource},
            "spec": {},
            "status": {
                "desiredNumberScheduled": 1,
                "numberReady": 1 if ready else 0,
                "numberAvailable": 1 if ready else 0,
            },
        }
    return {
        "metadata": {"name": resource},
        "spec": {"replicas": 1},
        "status": {"availableReplicas": 1 if ready else 0, "readyReplicas": 1 if ready else 0},
    }


def _make_runner(existing: set[str], applied: list[str], ready: bool = True):
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        if arguments[0] == "get":
            resource = arguments[2]
            kind = arguments[1]
            if resource in existing:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps(_workload(resource, kind, ready)),
                    "stderr": "",
                }
            return {"available": False, "returncode": 1, "stdout": "", "stderr": "NotFound"}
        if arguments[0] == "apply":
            if "--dry-run=server" not in arguments:
                applied.append(arguments[-1])
            return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}
        return {"available": False, "returncode": 1, "stderr": "unknown"}

    return runner, calls


def test_apply_marks_data_plane_and_skips_existing_without_manifest():
    plan = plan_network_stack(_spec())
    existing = {"calico-node"}
    applied: list[str] = []
    runner, calls = _make_runner(existing, applied)

    evidence = apply_network_stack(plan, NAMESPACE, runner)

    assert evidence["already_present_plugins"] == ["calico"]
    assert evidence["apply_pending_plugins"] == ["sriov"]
    assert evidence["data_plane_plugins"] == ["sriov"]
    assert evidence["applied_plugins"] == []
    assert evidence["side_effects"] is False
    assert not any(call[0] == "apply" for call in calls)


def test_apply_applies_supplied_manifest_for_missing_plugin():
    plan = plan_network_stack(_spec())
    applied: list[str] = []
    manifests = {
        "sriov": {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "sriov-device-plugin", "namespace": NAMESPACE},
        }
    }
    runner, _ = _make_runner(set(), applied)

    evidence = apply_network_stack(plan, NAMESPACE, runner, manifests=manifests)

    assert evidence["applied_plugins"] == ["sriov"]
    assert evidence["side_effects"] is True
    assert len(applied) == 1


def test_wait_ready_succeeds_for_enabled_plugins():
    plan = plan_network_stack(_spec())
    existing = {"calico-node", "sriov-device-plugin"}
    applied: list[str] = []
    runner, _ = _make_runner(existing, applied, ready=True)

    evidence = wait_for_network_stack_ready(plan, NAMESPACE, runner, timeout_seconds=60)

    assert evidence["ready_plugin_count"] == 2
    assert evidence["data_plane_plugins"] == ["sriov"]
    assert evidence["side_effects"] is False


def test_wait_ready_times_out_when_plugins_stay_not_ready():
    plan = plan_network_stack(_spec())
    existing = {"calico-node", "sriov-device-plugin"}
    applied: list[str] = []
    runner, _ = _make_runner(existing, applied, ready=False)
    ticks = {"value": 0}

    def monotonic() -> float:
        ticks["value"] += 100
        return float(ticks["value"])

    with pytest.raises(NetworkStackAdapterError, match="NETWORK_STACK_READY_TIMEOUT"):
        wait_for_network_stack_ready(
            plan,
            NAMESPACE,
            runner,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
            timeout_seconds=60,
        )


def test_apply_rejects_empty_plan():
    runner, _ = _make_runner(set(), [])
    with pytest.raises(NetworkStackAdapterError, match="NETWORK_STACK_PLAN_INVALID"):
        apply_network_stack([], NAMESPACE, runner)


def _document_runner(document: dict[str, object] | None, calls: list[list[str]]):
    def runner(arguments: list[str], **_kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        if arguments[0] == "get":
            if document is None:
                return {"available": False, "returncode": 1, "stdout": "", "stderr": "NotFound"}
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(document),
                "stderr": "",
            }
        if arguments[0] == "apply":
            return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}
        return {"available": False, "returncode": 1, "stderr": "unknown"}

    return runner


def test_observe_plugin_rejects_zero_replica_deployment():
    spec = {
        "plugins": {
            "calico": {
                "config": {"resource_name": "calico-node", "resource_kind": "Deployment"}
            },
        }
    }
    plan = plan_network_stack(spec)
    calico = plan[0]
    document = {
        "metadata": {"name": "calico-node"},
        "spec": {"replicas": 0},
        "status": {"availableReplicas": 0, "readyReplicas": 0},
    }
    from apps.target_agent.network_stack_adapter import observe_plugin

    observed = observe_plugin(calico, NAMESPACE, _document_runner(document, []))

    assert observed["desired"] == 0
    assert observed["ready"] is False


def test_observe_plugin_rejects_daemonset_scheduling_zero_nodes():
    from apps.target_agent.network_stack_adapter import observe_plugin

    plan = plan_network_stack(_spec())
    sriov = next(plugin for plugin in plan if plugin.name == "sriov")
    document = {
        "metadata": {"name": "sriov-device-plugin"},
        "spec": {},
        "status": {"desiredNumberScheduled": 0, "numberReady": 0, "numberAvailable": 0},
    }

    observed = observe_plugin(sriov, NAMESPACE, _document_runner(document, []))

    assert observed["ready"] is False


def test_wait_ready_times_out_when_daemonset_scheduled_zero_nodes():
    plan = plan_network_stack(_spec())
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 40.0
        return clock["now"]

    document = {
        "metadata": {"name": "calico-node"},
        "spec": {},
        "status": {"desiredNumberScheduled": 0, "numberReady": 0, "numberAvailable": 0},
    }
    with pytest.raises(NetworkStackAdapterError, match="NETWORK_STACK_READY_TIMEOUT"):
        wait_for_network_stack_ready(
            plan,
            NAMESPACE,
            _document_runner(document, []),
            sleep=lambda _seconds: None,
            monotonic=monotonic,
            timeout_seconds=60,
        )


def test_apply_fails_when_existence_check_hits_a_real_error():
    plan = plan_network_stack(_spec())
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        return {"available": True, "returncode": 1, "stdout": "", "stderr": "forbidden"}

    with pytest.raises(NetworkStackAdapterError, match="NETWORK_STACK_OPERATION_FAILED"):
        apply_network_stack(plan, NAMESPACE, runner)
    assert not any(call[0] == "apply" for call in calls)


def test_apply_fails_when_existing_workload_document_is_unparsable():
    plan = plan_network_stack(_spec())
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        if arguments[0] == "get":
            return {"available": True, "returncode": 0, "stdout": "not json", "stderr": ""}
        return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}

    with pytest.raises(NetworkStackAdapterError, match="NETWORK_STACK_WORKLOAD_JSON_INVALID"):
        apply_network_stack(plan, NAMESPACE, runner)
    assert not any(call[0] == "apply" for call in calls)
