import pytest

from apps.eph_agent.ntp_policy import (
    EPHNtpPolicyError,
    render_eph_chrony_config,
    render_eph_chrony_pre_kubeadm_command,
    validate_eph_bootstrap_ntp_sources,
)


def test_eph_bootstrap_uses_only_approved_external_sources():
    sources = validate_eph_bootstrap_ntp_sources(
        ["192.0.2.10", "ntp1.example.net"],
        ["ntp1.example.net", "192.0.2.10"],
    )
    assert sources == ("192.0.2.10", "ntp1.example.net")


def test_eph_bootstrap_rejects_unapproved_source():
    with pytest.raises(EPHNtpPolicyError, match="NOT_APPROVED"):
        validate_eph_bootstrap_ntp_sources(
            ["pool.ntp.org"],
            ["ntp1.example.net"],
        )


def test_eph_bootstrap_requires_at_least_one_approved_source():
    with pytest.raises(EPHNtpPolicyError, match="REQUIRED"):
        validate_eph_bootstrap_ntp_sources([], [])


def test_eph_bootstrap_rejects_duplicate_sources():
    with pytest.raises(EPHNtpPolicyError, match="DUPLICATE"):
        validate_eph_bootstrap_ntp_sources(
            ["ntp1.example.net", "ntp1.example.net"],
            ["ntp1.example.net"],
        )


@pytest.mark.parametrize(
    "source",
    ["127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "bad host"],
)
def test_eph_bootstrap_rejects_unroutable_or_invalid_sources(source):
    with pytest.raises(EPHNtpPolicyError):
        validate_eph_bootstrap_ntp_sources([source], [source])


def test_eph_chrony_config_is_client_only_and_uses_approved_sources():
    config = render_eph_chrony_config(["192.0.2.10", "ntp1.example.net"])
    assert "server 192.0.2.10 iburst" in config
    assert "server ntp1.example.net iburst" in config
    assert "pool " not in config
    assert "allow " not in config
    assert "port 123" not in config


def test_eph_pre_kubeadm_command_installs_enables_and_verifies_chrony():
    command = render_eph_chrony_pre_kubeadm_command(["192.0.2.10"])
    assert "apt-get install -y chrony" in command
    assert "zypper --non-interactive install chrony" in command
    assert "dnf install -y chrony" in command
    assert "yum install -y chrony" in command
    assert "systemctl enable" in command
    assert "systemctl restart" in command
    assert "chronyc tracking" in command
    assert "Leap status" in command
    assert "server 192.0.2.10 iburst" in command
    assert "pool " not in command
