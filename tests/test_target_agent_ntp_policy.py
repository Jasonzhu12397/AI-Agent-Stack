import pytest

from apps.target_agent.ntp_policy import (
    NtpPolicyError,
    render_worker_chrony_config,
    validate_worker_ntp_sources,
)


def test_worker_uses_only_ironic_internal_ntp_ip():
    assert validate_worker_ntp_sources("10.130.60.84", ["10.130.60.84"]) == ("10.130.60.84",)


def test_worker_rejects_external_ntp_fallback():
    with pytest.raises(NtpPolicyError, match="EXACTLY_ONE"):
        validate_worker_ntp_sources(
            "10.130.60.84",
            ["10.130.60.84", "time.google.com"],
        )


def test_worker_rejects_external_ntp_source():
    with pytest.raises(NtpPolicyError, match="MUST_BE_IRONIC_INTERNAL_IP"):
        validate_worker_ntp_sources("10.130.60.84", ["1.1.1.1"])


def test_worker_chrony_config_has_no_pool_or_external_fallback():
    config = render_worker_chrony_config("10.130.60.84")
    assert config.splitlines()[0] == "server 10.130.60.84 iburst prefer"
    assert "pool " not in config
    assert config.count("server ") == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0"])
def test_worker_rejects_non_routable_ntp_endpoint(address):
    with pytest.raises(NtpPolicyError):
        render_worker_chrony_config(address)
