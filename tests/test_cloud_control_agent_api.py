from uuid import uuid4

import pytest

from apps.cloud_control_agent.api import ControlApiError, _parse_uuid, _certificate_common_name


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
