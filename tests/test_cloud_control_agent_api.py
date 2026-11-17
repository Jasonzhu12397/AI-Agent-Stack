from uuid import uuid4

import pytest

from apps.cloud_control_agent.api import (
    ControlApiError,
    _certificate_common_name,
    _checkpoint_state_digest,
    _parse_uuid,
    _validate_action_role,
    _validate_checkpoint_actor_transition,
)


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



def test_eph_agent_can_persist_only_initial_single_node_checkpoint():
    _validate_checkpoint_actor_transition(
        "eph",
        "not_started",
        "eph_single_node_ready",
    )


@pytest.mark.parametrize(
    ("current_phase", "next_phase"),
    [
        ("not_started", "target_control_plane_ready"),
        ("eph_single_node_ready", "target_agent_ready"),
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
        "eph_single_node_ready",
        "target_control_plane_ready",
    )


@pytest.mark.parametrize(
    ("current_phase", "next_phase"),
    [
        ("not_started", "target_control_plane_ready"),
        ("eph_single_node_ready", "bmh_registry_migrated"),
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
        ("eph", "eph.capi.pivot"),
        ("target", "target.cluster.observe"),
        ("target", "target.handoff.evaluate"),
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
