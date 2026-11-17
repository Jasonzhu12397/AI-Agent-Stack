from uuid import uuid4

import pytest

from apps.cloud_control_agent.api import (
    ControlApiError,
    _certificate_common_name,
    _checkpoint_state_digest,
    _parse_uuid,
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
