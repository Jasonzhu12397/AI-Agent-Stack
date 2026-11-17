import pytest

from node_agent.delivery_lifecycle import (
    DeliveryLifecycleError,
    evaluate_transition,
    validate_bmh_migration,
    validate_chronyd_scope,
    validate_ntp_topology,
    validate_topology,
)


def topology():
    return {
        "eph_mode": "single_node",
        "eph_kubernetes_node_count": 1,
        "target_agent_placement": "target_control_plane",
        "chronyd_scope": "eph_and_target_kubernetes_nodes",
    }


def test_eph_is_exactly_one_kubernetes_node():
    result = validate_topology(topology())
    assert result["valid"] is True
    assert result["chronyd_scope"] == "eph_and_target_kubernetes_nodes"


@pytest.mark.parametrize("count", [0, 2, 3, True])
def test_rejects_non_single_node_eph(count):
    config = topology()
    config["eph_kubernetes_node_count"] = count
    with pytest.raises(DeliveryLifecycleError, match="EPH_MUST_BE_SINGLE_NODE"):
        validate_topology(config)


def test_rejects_chronyd_scope_that_omits_eph():
    config = topology()
    config["chronyd_scope"] = "target_workers_only"
    with pytest.raises(
        DeliveryLifecycleError,
        match="CHRONYD_SCOPE_MUST_INCLUDE_EPH_AND_TARGET_NODES",
    ):
        validate_topology(config)


@pytest.mark.parametrize(
    "role",
    ["eph", "target_control_plane", "target_worker"],
)
@pytest.mark.parametrize("action", ["install", "configure", "enable", "verify"])
def test_chronyd_is_allowed_for_all_kubernetes_node_roles(role, action):
    result = validate_chronyd_scope(role, action)
    assert result["allowed"] is True
    assert result["node_role"] == role


def test_eph_chronyd_must_run_before_kubeadm():
    result = validate_chronyd_scope("eph", "verify")
    assert result["required_before_kubeadm"] is True


def test_target_control_plane_chronyd_must_run_before_kubeadm():
    result = validate_chronyd_scope("target_control_plane", "verify")
    assert result["required_before_kubeadm"] is True


def test_worker_chronyd_must_run_before_node_join():
    result = validate_chronyd_scope("target_worker", "verify")
    assert result["required_before_node_join"] is True


def test_chronyd_rejects_unknown_role_or_action():
    with pytest.raises(DeliveryLifecycleError, match="CHRONYD_NODE_ROLE_INVALID"):
        validate_chronyd_scope("cloud_agent", "install")
    with pytest.raises(DeliveryLifecycleError, match="CHRONYD_ACTION_INVALID"):
        validate_chronyd_scope("eph", "remove")


def test_bmh_migration_requires_exact_identity_set_and_target_ownership():
    hosts = [
        {
            "name": "worker-01",
            "namespace": "metal3",
            "boot_mac": "00:11:22:33:44:55",
            "bmc_address": "redfish://bmc01",
            "serial_number": "SN01",
        },
        {
            "name": "worker-02",
            "namespace": "metal3",
            "boot_mac": "00:11:22:33:44:66",
            "bmc_address": "redfish://bmc02",
            "serial_number": "SN02",
        },
    ]
    result = validate_bmh_migration(
        hosts, list(hosts), target_controller_owns_inventory=True
    )
    assert result["migration_verified"] is True
    assert result["source_mutation_performed"] is False
    assert result["kubernetes_uids_used_as_identity"] is False


def test_bmh_migration_rejects_missing_target_host():
    source = [
        {
            "name": "worker-01",
            "namespace": "metal3",
            "boot_mac": "00:11:22:33:44:55",
        }
    ]
    with pytest.raises(
        DeliveryLifecycleError,
        match="BMH_REGISTRY_HARDWARE_IDENTITY_MISMATCH",
    ):
        validate_bmh_migration(
            source, [], target_controller_owns_inventory=True
        )


def test_bmh_migration_requires_target_controller_ownership():
    hosts = [
        {
            "name": "worker-01",
            "namespace": "metal3",
            "boot_mac": "00:11:22:33:44:55",
        }
    ]
    with pytest.raises(
        DeliveryLifecycleError,
        match="TARGET_BMH_CONTROLLER_OWNERSHIP_UNVERIFIED",
    ):
        validate_bmh_migration(
            hosts, list(hosts), target_controller_owns_inventory=False
        )


def test_target_agent_can_advance_without_cloud_approval():
    evidence = {
        "eph_agent_stopped": True,
        "eph_bootstrap_components_removed": True,
        "eph_credentials_revoked": True,
    }
    result = evaluate_transition(
        "bmh_registry_migrated", "eph_cleanup_verified", evidence
    )
    assert result["accepted"] is True
    assert result["cloud_approval_required"] is False
    assert result["eph_cleanup_allowed"] is True


def test_cleanup_cannot_precede_bmh_migration():
    with pytest.raises(
        DeliveryLifecycleError, match="DELIVERY_PHASE_TRANSITION_INVALID"
    ):
        evaluate_transition("capi_pivot_verified", "eph_cleanup_verified", {})


def test_worker_provisioning_requires_cleanup_phase():
    with pytest.raises(
        DeliveryLifecycleError, match="DELIVERY_PHASE_TRANSITION_INVALID"
    ):
        evaluate_transition("bmh_registry_migrated", "worker_provisioning", {})


def test_target_agent_command_exposes_lifecycle_policy():
    from node_agent.agent import execute_command

    result = execute_command(
        {
            "command_type": "cluster.delivery.lifecycle.evaluate",
            "payload": {
                "action": "chronyd.validate",
                "node_role": "eph",
                "chronyd_action": "install",
            },
        },
        "target_cluster",
    )
    assert result["allowed"] is True
    assert result["required_before_kubeadm"] is True


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


@pytest.mark.parametrize(
    "hosts",
    [
        [
            {
                "name": "worker-01",
                "namespace": "metal3",
                "boot_mac": "00:11:22:33:44:55",
            },
            {
                "name": "worker-01",
                "namespace": "metal3",
                "boot_mac": "00:11:22:33:44:66",
            },
        ],
        [
            {
                "name": "worker-01",
                "namespace": "metal3",
                "boot_mac": "00:11:22:33:44:55",
            },
            {
                "name": "worker-02",
                "namespace": "metal3",
                "boot_mac": "00:11:22:33:44:55",
            },
        ],
    ],
)
def test_bmh_migration_rejects_duplicate_hardware_identity(hosts):
    with pytest.raises(
        DeliveryLifecycleError, match="BMH_IDENTITY_DUPLICATED"
    ):
        validate_bmh_migration(
            hosts, hosts, target_controller_owns_inventory=True
        )


def test_control_plane_uses_external_ntp_sources():
    result = validate_ntp_topology(
        "target_control_plane",
        ["time.example.net", "192.0.2.10"],
        source_scope="external",
    )
    assert result["serves_internal_worker_ntp"] is True


def test_worker_uses_only_ironic_internal_ntp_ip():
    result = validate_ntp_topology(
        "target_worker",
        ["10.130.60.84"],
        source_scope="ironic_internal",
        ironic_internal_ip="10.130.60.84",
    )
    assert result["external_ntp_allowed"] is False


@pytest.mark.parametrize(
    "sources,scope,ironic_ip",
    [
        (["pool.ntp.org"], "external", "10.130.60.84"),
        (["192.0.2.10"], "external", "10.130.60.84"),
        (["10.130.60.84", "pool.ntp.org"], "ironic_internal", "10.130.60.84"),
        (["10.130.60.85"], "ironic_internal", "10.130.60.84"),
        (["10.130.60.84"], "external", "10.130.60.84"),
    ],
)
def test_worker_rejects_non_ironic_or_external_ntp(sources, scope, ironic_ip):
    with pytest.raises(DeliveryLifecycleError):
        validate_ntp_topology(
            "target_worker",
            sources,
            source_scope=scope,
            ironic_internal_ip=ironic_ip,
        )


def test_control_plane_rejects_worker_internal_ntp_scope():
    with pytest.raises(
        DeliveryLifecycleError,
        match="CONTROL_PLANE_NTP_MUST_USE_EXTERNAL_SOURCES",
    ):
        validate_ntp_topology(
            "target_control_plane",
            ["10.130.60.84"],
            source_scope="ironic_internal",
        )
