import pytest

from apps.target_agent.handoff import HandoffError, evaluate_transition


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
            "eph_single_node_ready",
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
        "eph_single_node_ready",
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



def test_handoff_completion_is_blocked_without_live_postflight_executor():
    from apps.target_agent.main import execute

    outcome = execute({
        "action": "target.handoff.evaluate",
        "payload": {
            "next_phase": "completed",
            "evidence": {
                "target_postflight_passed": True,
                "worker_network_postflight_passed": True,
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
    assert outcome["error_code"] == "TARGET_POSTFLIGHT_EXECUTOR_NOT_REGISTERED"
