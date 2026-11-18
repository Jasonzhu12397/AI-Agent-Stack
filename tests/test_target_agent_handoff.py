import pytest

from apps.target_agent.handoff import (
    HandoffError,
    HandoffPhase,
    evaluate_transition,
    validate_phase_evidence,
)


def test_handoff_requires_bmh_registration_and_inspected_readiness():
    with pytest.raises(HandoffError, match="BMH_REGISTRATION_INVENTORY_INCOMPLETE"):
        evaluate_transition(
            "eph_single_node_ready",
            "bmh_registered",
            {
                "bmh_registration_applied": True,
                "bmh_secret_refs_verified": True,
                "bmh_count": 2,
                "bmh_inventory_count": 1,
            },
        )
    with pytest.raises(HandoffError, match="BMH_READINESS_INCOMPLETE"):
        evaluate_transition(
            "bmh_registered",
            "bmh_ready",
            {
                "expected_bmh_count": 2,
                "bmh_ready_count": 1,
                "bmh_all_operational": True,
                "ironic_inventory_verified": True,
            },
        )


def test_handoff_rejects_skipped_phase():
    with pytest.raises(HandoffError, match="TRANSITION_INVALID"):
        evaluate_transition(
            None,
            "target_control_plane_ready",
            {"target_control_plane_ready": True, "ready_master_count": 3},
        )


def test_handoff_requires_single_node_eph():
    with pytest.raises(HandoffError, match="SINGLE_NODE"):
        evaluate_transition(
            None,
            "eph_single_node_ready",
            {
                "eph_ready": True,
                "eph_node_count": 2,
                "eph_source_preflight_passed": True,
            },
        )


def test_handoff_requires_three_ready_target_masters_and_internal_ntp():
    with pytest.raises(HandoffError, match="THREE_TARGET_MASTERS"):
        evaluate_transition(
            "bmh_ready",
            "target_control_plane_ready",
            {
                "target_control_plane_ready": True,
                "ready_master_count": 2,
                "ironic_internal_ntp_ip": "10.130.60.84",
                "ironic_internal_ntp_service_reachable": True,
            },
        )


def test_handoff_rejects_bmh_identity_set_mismatch():
    with pytest.raises(HandoffError, match="BMH_IDENTITY_SETS_DIFFER"):
        evaluate_transition(
            "capi_pivot_verified",
            "bmh_registry_migrated",
            {
                "source_bmh_identities": [
                    {"namespace": "metal3", "name": "host-1", "hardware_identity": "abc"}
                ],
                "target_bmh_identities": [
                    {"namespace": "metal3", "name": "host-2", "hardware_identity": "abc"}
                ],
                "target_metal3_ownership_verified": True,
                "target_metal3_reconciliation_healthy": True,
                "target_ironic_inventory_verified": True,
            },
        )


def test_handoff_rejects_worker_external_ntp_fallback():
    with pytest.raises(HandoffError, match="NTP_SOURCE_MISMATCH"):
        evaluate_transition(
            "worker_provisioning",
            "workers_ready",
            {
                "ironic_internal_ntp_ip": "10.130.60.84",
                "desired_worker_count": 1,
                "ready_worker_count": 1,
                "worker_ntp_bootstrap_sources": {"worker-1": ["1.1.1.1"]},
            },
        )


def test_handoff_accepts_complete_ordered_worker_evidence():
    result = evaluate_transition(
        "worker_provisioning",
        "workers_ready",
        {
            "ironic_internal_ntp_ip": "10.130.60.84",
            "desired_worker_count": 2,
            "ready_worker_count": 2,
            "worker_ntp_bootstrap_sources": {
                "worker-1": ["10.130.60.84"],
                "worker-2": ["10.130.60.84"],
            },
        },
    )
    assert result["accepted"] is True
    assert result["side_effects"] is False



def test_capi_pivot_accepts_new_target_uid_with_preserved_source_identity_annotation():
    result = evaluate_transition(
        "target_agent_ready",
        "capi_pivot_verified",
        {
            "capi_pivot_succeeded": True,
            "target_controllers_ready": True,
            "source_cluster_uid": "source-api-uid",
            "target_cluster_uid": "target-api-uid",
            "target_source_cluster_uid_annotation": "source-api-uid",
            "source_stable_cluster_identity": "cluster-logical-id-123",
            "target_stable_cluster_identity": "cluster-logical-id-123",
            "source_cluster_absent": True,
            "target_cluster_ownership_verified": True,
        },
    )
    assert result["accepted"] is True
    assert result["evidence"]["source_cluster_uid"] != result["evidence"]["target_cluster_uid"]


def test_capi_pivot_rejects_source_uid_annotation_mismatch():
    with pytest.raises(HandoffError, match="SOURCE_UID_ANNOTATION_MISMATCH"):
        evaluate_transition(
            "target_agent_ready",
            "capi_pivot_verified",
            {
                "capi_pivot_succeeded": True,
                "target_controllers_ready": True,
                "source_cluster_uid": "source-api-uid",
                "target_cluster_uid": "target-api-uid",
                "target_source_cluster_uid_annotation": "wrong-source-uid",
                "source_stable_cluster_identity": "cluster-logical-id-123",
                "target_stable_cluster_identity": "cluster-logical-id-123",
                "source_cluster_absent": True,
                "target_cluster_ownership_verified": True,
            },
        )



def test_handoff_blocks_workers_until_control_plane_chronyd_is_verified():
    with pytest.raises(
        HandoffError,
        match="EVIDENCE_REQUIRED:target_control_plane_ntp_profile_verified",
    ):
        evaluate_transition(
            "eph_single_node_ready",
            "target_control_plane_ready",
            {
                "target_control_plane_ready": True,
                "ready_master_count": 3,
                "ironic_internal_ntp_ip": "10.130.60.84",
                "ironic_internal_ntp_service_reachable": True,
            },
        )


def test_handoff_accepts_target_control_plane_only_after_external_ntp_sync():
    result = evaluate_transition(
        "bmh_ready",
        "target_control_plane_ready",
        {
            "target_control_plane_ready": True,
            "ready_master_count": 3,
            "ironic_internal_ntp_ip": "10.130.60.84",
            "target_control_plane_ntp_profile_verified": True,
            "target_control_plane_ntp_bootstrap_sync_gate_verified": True,
            "ironic_internal_ntp_service_reachable": True,
        },
    )
    assert result["accepted"] is True



def test_handoff_completion_requires_durable_workers_ready_checkpoint():
    from apps.target_agent.main import execute

    outcome = execute({
        "action": "target.handoff.evaluate",
        "deployment_id": "deployment-1",
        "payload": {
            "next_phase": "completed",
            "evidence": {
                "target_postflight_passed": True,
                "worker_network_postflight_passed": True,
                "worker_chronyd_bootstrap_sync_verified": True,
                "data_plane_pod_packet_path_verified": True,
                "deployment_checkpoint_durable": True,
            },
        },
        "deployment_checkpoint": {
            "phase": "workers_ready",
            "sequence": 8,
            "state": {},
        },
    })
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "HANDOFF_DURABLE_CHECKPOINT_EVIDENCE_INVALID"


def test_handoff_completion_rejects_unverified_data_plane_packet_path():
    with pytest.raises(
        HandoffError,
        match="HANDOFF_EVIDENCE_REQUIRED:data_plane_pod_packet_path_verified",
    ):
        validate_phase_evidence(
            HandoffPhase.COMPLETED,
            {
                "target_postflight_passed": True,
                "worker_network_postflight_passed": True,
                "worker_chronyd_bootstrap_sync_verified": True,
                "data_plane_pod_packet_path_verified": False,
                "deployment_checkpoint_durable": True,
            },
        )


def test_handoff_requires_applied_worker_resources_and_validated_network_contract():
    with pytest.raises(
        HandoffError,
        match="EVIDENCE_REQUIRED:worker_provisioning_resources_applied",
    ):
        evaluate_transition(
            "eph_cleanup_verified",
            "worker_provisioning",
            {
                "ironic_internal_ntp_ip": "10.130.60.84",
                "ironic_internal_ntp_service_reachable": True,
                "target_agent_leader_elected": True,
            },
        )


def test_handoff_accepts_worker_provisioning_only_after_server_dry_run_and_controller_observation():
    result = evaluate_transition(
        "eph_cleanup_verified",
        "worker_provisioning",
        {
            "ironic_internal_ntp_ip": "10.130.60.84",
            "ironic_internal_ntp_service_reachable": True,
            "target_agent_leader_elected": True,
            "worker_provisioning_resources_applied": True,
            "worker_provisioning_server_dry_run_passed": True,
            "worker_provisioning_controller_observed": True,
            "worker_network_contract_validated": True,
            "preprovision_bmh_capacity_verified": True,
            "desired_worker_count": 10,
        },
    )
    assert result["accepted"] is True
    assert result["evidence"]["desired_worker_count"] == 10
