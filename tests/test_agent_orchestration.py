from uuid import uuid4

import pytest

from packages.agent_contracts.orchestration import (
    DeploymentPhase,
    OrchestrationError,
    build_idempotency_key,
    build_payload,
    is_terminal,
    next_phase_after,
    phase_index,
    plan_next_step,
    role_for_action,
)


def test_phase_index_matches_lifecycle_order():
    assert phase_index(DeploymentPhase.NOT_STARTED) == -1
    assert phase_index(DeploymentPhase.EPH_SINGLE_NODE_READY) == 0
    assert phase_index(DeploymentPhase.BMH_READY) == 2
    assert phase_index(DeploymentPhase.COMPLETED) == 10


def test_terminal_phase_plans_no_next_step():
    deployment_id = uuid4()
    plan = plan_next_step(deployment_id, DeploymentPhase.COMPLETED, {})
    assert plan is None
    assert is_terminal(DeploymentPhase.COMPLETED)
    assert next_phase_after(DeploymentPhase.COMPLETED) is None


def test_full_lifecycle_dispatch_chain_is_monotonic():
    deployment_id = uuid4()
    spec = {"cluster_name": "edge-01", "worker_replicas": 3}
    phase = DeploymentPhase.NOT_STARTED
    visited: list[tuple[str, str, str]] = []

    while not is_terminal(phase):
        plan = plan_next_step(deployment_id, phase, spec)
        assert plan is not None
        visited.append((plan.role, plan.action, plan.next_phase.value))
        assert phase_index(plan.next_phase) == phase_index(phase) + 1
        phase = plan.next_phase

    assert visited == [
        ("eph", "eph.preflight", "eph_single_node_ready"),
        ("eph", "eph.metal3.bmh.apply", "bmh_registered"),
        ("eph", "eph.metal3.bmh.wait_ready", "bmh_ready"),
        ("eph", "eph.capi.cluster.apply", "target_control_plane_ready"),
        ("target", "target.handoff.evaluate", "target_agent_ready"),
        ("target", "target.capi.pivot", "capi_pivot_verified"),
        ("target", "target.metal3.verify_ownership", "bmh_registry_migrated"),
        ("target", "target.eph.cleanup", "eph_cleanup_verified"),
        ("target", "target.worker.provision", "worker_provisioning"),
        ("target", "target.handoff.evaluate", "workers_ready"),
        ("target", "target.handoff.evaluate", "completed"),
    ]


def test_role_for_action_resolves_eph_and_target():
    assert role_for_action("eph.preflight") == "eph"
    assert role_for_action("target.capi.pivot") == "target"
    with pytest.raises(OrchestrationError):
        role_for_action("cloud_control.deploy")


def test_idempotency_key_is_stable_per_deployment_action_phase():
    deployment_id = uuid4()
    first = build_idempotency_key(deployment_id, "eph.preflight", "eph_single_node_ready")
    second = build_idempotency_key(deployment_id, "eph.preflight", "eph_single_node_ready")
    other = build_idempotency_key(uuid4(), "eph.preflight", "eph_single_node_ready")
    assert first == second
    assert first != other
    assert first.startswith(f"ai-agent-stack:{deployment_id}:")


def test_plan_payload_carries_deployment_and_action():
    deployment_id = uuid4()
    plan = plan_next_step(deployment_id, DeploymentPhase.NOT_STARTED, {"cluster_name": "edge-01"})
    assert plan is not None
    assert plan.payload["deployment_id"] == str(deployment_id)
    assert plan.payload["action"] == "eph.preflight"
    assert plan.payload["cluster_name"] == "edge-01"


def test_build_payload_copies_typed_spec_fields_and_omits_missing():
    spec = {
        "cluster_name": "edge-01",
        "namespace": "metal3",
        "worker_replicas": 3,
        "worker_provisioning": {"machine_deployment_name": "edge-workers"},
        "hosts": [{"name": "bmh-1"}],
        "unknown_block": {"nested": "ignored"},
        "note": "free text",
    }
    payload = build_payload(uuid4(), "target.worker.provision", spec)
    assert payload["cluster_name"] == "edge-01"
    assert payload["namespace"] == "metal3"
    assert payload["worker_replicas"] == 3
    assert payload["worker_provisioning"] == {"machine_deployment_name": "edge-workers"}
    assert payload["hosts"] == [{"name": "bmh-1"}]
    assert "unknown_block" not in payload
    assert "note" not in payload


def test_build_payload_rejects_non_mapping_spec():
    with pytest.raises(OrchestrationError):
        build_payload(uuid4(), "eph.preflight", ["not", "a", "dict"])


def test_plan_next_step_rejects_unknown_phase():
    with pytest.raises(OrchestrationError):
        plan_next_step(uuid4(), "phase-does-not-exist", {})


def test_plan_next_step_rejects_non_mapping_spec():
    with pytest.raises(OrchestrationError):
        plan_next_step(uuid4(), DeploymentPhase.NOT_STARTED, ["bad"])
