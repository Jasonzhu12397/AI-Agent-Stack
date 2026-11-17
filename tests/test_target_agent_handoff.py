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
                    {"namespace": "metal3", "name": "host-1", "system_uuid": "abc"}
                ],
                "target_bmh_identities": [
                    {"namespace": "metal3", "name": "host-2", "system_uuid": "abc"}
                ],
                "target_metal3_ownership_verified": True,
                "target_metal3_reconciliation_healthy": True,
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
                "worker_ntp_sources": {"worker-1": ["1.1.1.1"]},
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
            "worker_ntp_sources": {
                "worker-1": ["10.130.60.84"],
                "worker-2": ["10.130.60.84"],
            },
        },
    )
    assert result["accepted"] is True
    assert result["side_effects"] is False
