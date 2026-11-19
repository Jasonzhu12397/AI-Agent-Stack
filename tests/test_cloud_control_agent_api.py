from uuid import uuid4

import pytest

from apps.cloud_control_agent.api import (
    ControlApiError,
    _certificate_common_name,
    _checkpoint_state_digest,
    _parse_uuid,
    _validate_action_role,
    _validate_checkpoint_actor_transition,
    _validate_reported_capabilities,
    _validated_eph_pivot_attestation,
)


def test_agent_lifecycle_pivot_action_is_allowed_without_approval_contract():
    _validate_action_role("target", "target.capi.pivot")


def test_cloud_control_exposes_metal3_and_capi_deployment_actions():
    for action in (
        "eph.metal3.bmh.apply",
        "eph.metal3.bmh.observe",
        "eph.metal3.bmh.wait_ready",
        "eph.capi.cluster.apply",
        "eph.capi.cluster.observe",
        "eph.capi.cluster.wait_ready",
    ):
        _validate_action_role("eph", action)



def test_parse_uuid_rejects_invalid_identity():
    with pytest.raises(ControlApiError):
        _parse_uuid("not-a-uuid", "AGENT_ID")


def test_parse_uuid_accepts_uuid():
    value = uuid4()
    assert _parse_uuid(str(value), "AGENT_ID") == value


def test_certificate_common_name_extracts_agent_identity():
    certificate = {"subject": ((("commonName", str(uuid4())),),)}
    assert _certificate_common_name(certificate) == certificate["subject"][0][0][1]


def test_certificate_common_name_rejects_missing_certificate():
    assert _certificate_common_name(None) is None



def test_checkpoint_digest_is_canonical_and_order_independent():
    first = {"worker": "worker-1", "ntp": ["10.130.60.84"], "ready": True}
    second = {"ready": True, "ntp": ["10.130.60.84"], "worker": "worker-1"}
    assert _checkpoint_state_digest(first) == _checkpoint_state_digest(second)


def test_checkpoint_digest_changes_when_evidence_changes():
    first = {"worker": "worker-1", "ntp": ["10.130.60.84"]}
    second = {"worker": "worker-1", "ntp": ["1.1.1.1"]}
    assert _checkpoint_state_digest(first) != _checkpoint_state_digest(second)



def test_eph_agent_can_persist_bootstrap_and_metal3_checkpoints():
    _validate_checkpoint_actor_transition("eph", "not_started", "eph_single_node_ready")
    _validate_checkpoint_actor_transition("eph", "eph_single_node_ready", "bmh_registered")
    _validate_checkpoint_actor_transition("eph", "bmh_registered", "bmh_ready")


@pytest.mark.parametrize(
    ("current_phase", "next_phase"),
    [
        ("not_started", "target_control_plane_ready"),
        ("eph_single_node_ready", "target_agent_ready"),
        ("bmh_ready", "target_control_plane_ready"),
        ("eph_single_node_ready", "eph_cleanup_verified"),
        ("workers_ready", "completed"),
    ],
)
def test_eph_agent_cannot_write_target_owned_or_skipped_checkpoints(
    current_phase,
    next_phase,
):
    with pytest.raises(ControlApiError):
        _validate_checkpoint_actor_transition("eph", current_phase, next_phase)


def test_target_agent_can_advance_from_eph_checkpoint_in_order():
    _validate_checkpoint_actor_transition(
        "target",
        "bmh_ready",
        "target_control_plane_ready",
    )


@pytest.mark.parametrize(
    ("current_phase", "next_phase"),
    [
        ("not_started", "target_control_plane_ready"),
        ("eph_single_node_ready", "bmh_registry_migrated"),
        ("bmh_ready", "capi_pivot_verified"),
        ("target_agent_ready", "eph_single_node_ready"),
        ("completed", "completed"),
    ],
)
def test_target_agent_cannot_skip_or_rewrite_checkpoint_phases(
    current_phase,
    next_phase,
):
    with pytest.raises(ControlApiError):
        _validate_checkpoint_actor_transition("target", current_phase, next_phase)



@pytest.mark.parametrize(
    ("role", "action"),
    [
        ("eph", "eph.preflight"),
        ("target", "target.capi.pivot"),
        ("target", "target.capi.pivot"),
        ("target", "target.cluster.observe"),
        ("target", "target.handoff.evaluate"),
        ("target", "target.eph.cleanup"),
        ("target", "target.worker.provision"),
    ],
)
def test_cloud_control_accepts_only_role_owned_agent_capabilities(role, action):
    _validate_action_role(role, action)


@pytest.mark.parametrize(
    ("role", "action"),
    [
        ("eph", "target.handoff.evaluate"),
        ("eph", "target.worker.provision"),
        ("target", "eph.capi.pivot"),
        ("target", "run.shell"),
        ("cloud", "target.cluster.observe"),
    ],
)
def test_cloud_control_rejects_cross_role_or_unregistered_agent_capabilities(
    role,
    action,
):
    with pytest.raises(ControlApiError, match="ACTION_NOT_ALLOWED"):
        _validate_action_role(role, action)



def test_cloud_control_accepts_only_capabilities_owned_by_agent_role():
    assert _validate_reported_capabilities(
        "eph",
        ["eph.preflight", "eph.metal3.bmh.apply"],
    ) == ["eph.preflight", "eph.metal3.bmh.apply"]
    assert _validate_reported_capabilities(
        "target",
        [
            "target.cluster.observe",
            "target.handoff.evaluate",
            "target.eph.cleanup",
            "target.worker.provision",
        ],
    ) == [
        "target.cluster.observe",
        "target.handoff.evaluate",
        "target.eph.cleanup",
        "target.worker.provision",
    ]


@pytest.mark.parametrize(
    ("role", "capabilities"),
    [
        ("eph", ["target.handoff.evaluate"]),
        ("target", ["eph.capi.pivot"]),
        ("target", ["run.shell"]),
        ("target", ["target.cluster.observe", "target.cluster.observe"]),
        ("target", ["target.cluster.observe", None]),
    ],
)
def test_cloud_control_rejects_invalid_or_cross_role_heartbeat_capabilities(
    role,
    capabilities,
):
    with pytest.raises(ControlApiError):
        _validate_reported_capabilities(role, capabilities)



def test_eph_pivot_attestation_requires_matching_source_and_target_bmh_digests():
    import hashlib
    import json

    identities = [
        {
            "namespace": "metal3",
            "name": "host-1",
            "hardware_identity": "system-uuid:sha256:abc",
        },
        {
            "namespace": "metal3",
            "name": "host-2",
            "hardware_identity": "system-uuid:sha256:def",
        },
    ]
    digest = hashlib.sha256(
        json.dumps(
            identities,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    output = {
        "capi_pivot_succeeded": True,
        "source_cluster_absent": True,
        "target_cluster_ownership_verified": True,
        "target_controllers_ready": True,
        "cleanup_scope_attested": True,
        "target_observer_secret_source_uid_attested": True,
        "source_cleanup_resource_count": 7,
        "source_cluster_uid": "source-cluster-uid",
        "target_cluster_uid": "target-cluster-uid",
        "target_source_cluster_uid_annotation": "source-cluster-uid",
        "source_bmh_identities": identities,
        "target_bmh_identities": identities,
        "source_bmh_digest": digest,
        "target_bmh_digest": digest,
    }
    attestation = _validated_eph_pivot_attestation(output)
    assert attestation is not None
    assert attestation["source_bmh_digest"] == digest
    assert attestation["source_bmh_identities"] == identities


def test_eph_pivot_attestation_rejects_identity_or_digest_tampering():
    output = {
        "capi_pivot_succeeded": True,
        "source_cluster_absent": True,
        "target_cluster_ownership_verified": True,
        "target_controllers_ready": True,
        "cleanup_scope_attested": True,
        "target_observer_secret_source_uid_attested": True,
        "source_cleanup_resource_count": 7,
        "source_cluster_uid": "source-cluster-uid",
        "target_cluster_uid": "target-cluster-uid",
        "target_source_cluster_uid_annotation": "source-cluster-uid",
        "source_bmh_identities": [
            {
                "namespace": "metal3",
                "name": "host-1",
                "hardware_identity": "system-uuid:sha256:abc",
            }
        ],
        "target_bmh_identities": [
            {
                "namespace": "metal3",
                "name": "host-1",
                "hardware_identity": "system-uuid:sha256:abc",
            }
        ],
        "source_bmh_digest": "0" * 64,
        "target_bmh_digest": "0" * 64,
    }
    assert _validated_eph_pivot_attestation(output) is None


def test_cloud_control_accepts_complete_attestation_from_verified_pivot_replay():
    import hashlib
    import json

    identities = [{
        "namespace": "metal3",
        "name": "host-1",
        "hardware_identity": "system-uuid:sha256:abc",
    }]
    digest = hashlib.sha256(
        json.dumps(
            identities,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    output = {
        "already_pivoted": True,
        "capi_pivot_succeeded": True,
        "source_cluster_absent": True,
        "target_cluster_ownership_verified": True,
        "target_controllers_ready": True,
        "cleanup_scope_attested": True,
        "target_observer_secret_source_uid_attested": True,
        "source_cleanup_resource_count": 7,
        "source_cluster_uid": "source-cluster-uid",
        "target_cluster_uid": "target-cluster-uid",
        "target_source_cluster_uid_annotation": "source-cluster-uid",
        "source_bmh_identities": identities,
        "target_bmh_identities": identities,
        "source_bmh_digest": digest,
        "target_bmh_digest": digest,
    }
    assert _validated_eph_pivot_attestation(output) is not None


def test_eph_pivot_attestation_rejects_duplicate_hardware_identity():
    import hashlib
    import json

    identities = [
        {
            "namespace": "metal3",
            "name": "host-1",
            "hardware_identity": "system-uuid:sha256:abc",
        },
        {
            "namespace": "metal3",
            "name": "host-2",
            "hardware_identity": "system-uuid:sha256:abc",
        },
    ]
    digest = hashlib.sha256(
        json.dumps(
            identities,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert _validated_eph_pivot_attestation({
        "capi_pivot_succeeded": True,
        "source_cluster_absent": True,
        "target_cluster_ownership_verified": True,
        "source_cluster_uid": "source-cluster-uid",
        "target_cluster_uid": "target-cluster-uid",
        "source_bmh_identities": identities,
        "target_bmh_identities": identities,
        "source_bmh_digest": digest,
        "target_bmh_digest": digest,
    }) is None

