import pytest

from node_agent.delivery_lifecycle import (
    DeliveryLifecycleError,
    evaluate_transition,
    validate_bmh_migration,
    validate_chronyd_scope,
    validate_topology,
)


def test_eph_is_exactly_one_kubernetes_node():
    result = validate_topology({
        "eph_mode": "single_node",
        "eph_kubernetes_node_count": 1,
        "target_agent_placement": "target_control_plane",
        "worker_chronyd_scope": "target_workers_only",
    })
    assert result["valid"] is True


@pytest.mark.parametrize("count", [0, 2, 3])
def test_rejects_non_single_node_eph(count):
    with pytest.raises(DeliveryLifecycleError, match="EPH_MUST_BE_SINGLE_NODE"):
        validate_topology({
            "eph_mode": "single_node",
            "eph_kubernetes_node_count": count,
            "target_agent_placement": "target_control_plane",
            "worker_chronyd_scope": "target_workers_only",
        })


def test_chronyd_is_rejected_for_eph():
    with pytest.raises(DeliveryLifecycleError, match="CHRONYD_NOT_APPLICABLE_TO_EPH"):
        validate_chronyd_scope("eph", "install")


def test_chronyd_is_only_allowed_for_target_workers():
    assert validate_chronyd_scope("target_worker", "install")["allowed"] is True
    with pytest.raises(DeliveryLifecycleError, match="CHRONYD_ONLY_ALLOWED_ON_TARGET_WORKERS"):
        validate_chronyd_scope("target_control_plane", "install")


def test_bmh_migration_requires_exact_identity_set_and_target_ownership():
    hosts = [{"name": "worker-01", "uid": "uid-01"}, {"name": "worker-02", "uid": "uid-02"}]
    result = validate_bmh_migration(hosts, list(hosts), target_controller_owns_inventory=True)
    assert result["migration_verified"] is True
    assert result["source_mutation_performed"] is False


def test_bmh_migration_rejects_missing_target_host():
    source = [{"name": "worker-01", "uid": "uid-01"}]
    with pytest.raises(DeliveryLifecycleError, match="BMH_REGISTRY_IDENTITY_MISMATCH"):
        validate_bmh_migration(source, [], target_controller_owns_inventory=True)


def test_bmh_migration_requires_target_controller_ownership():
    hosts = [{"name": "worker-01", "uid": "uid-01"}]
    with pytest.raises(DeliveryLifecycleError, match="TARGET_BMH_CONTROLLER_OWNERSHIP_UNVERIFIED"):
        validate_bmh_migration(hosts, list(hosts), target_controller_owns_inventory=False)


def test_target_agent_can_advance_without_cloud_approval():
    evidence = {
        "eph_agent_stopped": True,
        "eph_bootstrap_components_removed": True,
        "eph_credentials_revoked": True,
    }
    result = evaluate_transition(
        "bmh_registry_migrated",
        "eph_cleanup_verified",
        evidence,
    )
    assert result["accepted"] is True
    assert result["cloud_approval_required"] is False
    assert result["eph_cleanup_allowed"] is True


def test_transition_does_not_require_cloud_approval():
    evidence = {
        "eph_agent_stopped": True,
        "eph_bootstrap_components_removed": True,
        "eph_credentials_revoked": True,
    }
    result = evaluate_transition(
        "bmh_registry_migrated",
        "eph_cleanup_verified",
        evidence,
    )
    assert result["accepted"] is True
    assert result["cloud_approval_required"] is False


def test_cleanup_cannot_precede_bmh_migration():
    with pytest.raises(DeliveryLifecycleError, match="DELIVERY_PHASE_TRANSITION_INVALID"):
        evaluate_transition("capi_pivot_verified", "eph_cleanup_verified", {})


def test_worker_provisioning_requires_cleanup_phase():
    with pytest.raises(DeliveryLifecycleError, match="DELIVERY_PHASE_TRANSITION_INVALID"):
        evaluate_transition("bmh_registry_migrated", "worker_provisioning", {})


def test_target_agent_command_exposes_lifecycle_policy():
    from node_agent.agent import execute_command

    result = execute_command(
        {
            "command_type": "cluster.delivery.lifecycle.evaluate",
            "payload": {
                "action": "transition.evaluate",
                "current_phase": "bmh_registry_migrated",
                "requested_phase": "eph_cleanup_verified",
                "evidence": {
                    "eph_agent_stopped": True,
                    "eph_bootstrap_components_removed": True,
                    "eph_credentials_revoked": True,
                },
            },
        },
        "target_cluster",
    )
    assert result["accepted"] is True
    assert result["eph_cleanup_allowed"] is True
    assert result["cloud_approval_required"] is False


def test_eph_agent_cannot_run_target_lifecycle_handoff_command():
    from node_agent.agent import NodeAgentError, execute_command

    with pytest.raises(NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        execute_command(
            {
                "command_type": "cluster.delivery.lifecycle.evaluate",
                "payload": {
                    "action": "transition.evaluate",
                    "current_phase": "bmh_registry_migrated",
                    "requested_phase": "eph_cleanup_verified",
                    "evidence": {
                        "eph_agent_stopped": True,
                        "eph_bootstrap_components_removed": True,
                        "eph_credentials_revoked": True,
                    },
                },
            },
            "eph_node",
        )


def test_rejects_boolean_as_eph_node_count():
    with pytest.raises(DeliveryLifecycleError, match="EPH_MUST_BE_SINGLE_NODE"):
        validate_topology({
            "eph_mode": "single_node",
            "eph_kubernetes_node_count": True,
            "target_agent_placement": "target_control_plane",
            "worker_chronyd_scope": "target_workers_only",
        })


@pytest.mark.parametrize(
    "hosts",
    [
        [{"name": "worker-01", "uid": "uid-01"}, {"name": "worker-01", "uid": "uid-02"}],
        [{"name": "worker-01", "uid": "uid-01"}, {"name": "worker-02", "uid": "uid-01"}],
    ],
)
def test_bmh_migration_rejects_duplicate_name_or_uid(hosts):
    with pytest.raises(DeliveryLifecycleError, match="BMH_IDENTITY_DUPLICATED"):
        validate_bmh_migration(hosts, hosts, target_controller_owns_inventory=True)
