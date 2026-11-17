import pytest

from apps.target_agent import ntp_policy
from apps.target_agent.ntp_policy import (
    NtpPolicyError,
    render_control_plane_chrony_config,
    render_control_plane_chrony_pre_kubeadm_command,
    render_worker_chrony_config,
    render_worker_chrony_pre_kubeadm_command,
    validate_control_plane_ntp_profile,
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



def test_worker_pre_kubeadm_command_installs_chrony_and_verifies_exact_internal_source():
    command = render_worker_chrony_pre_kubeadm_command("10.130.60.84")
    assert "NTP_SERVER='10.130.60.84'" in command
    assert "server 10.130.60.84 iburst prefer" in command
    assert "server $NTP_SERVER" not in command
    assert "apt-get install -y chrony" in command
    assert "zypper --non-interactive install chrony" in command
    assert "dnf install -y chrony" in command
    assert 'chronyc -n sources' in command
    assert 'chronyc tracking' in command
    assert "pool " not in command
    assert sum(line.startswith("server ") for line in command.splitlines()) == 1
    assert "did not synchronize with the required Ironic internal NTP endpoint" in command



def test_control_plane_ntp_profile_accepts_only_approved_external_sources():
    sources, networks = validate_control_plane_ntp_profile(
        ["192.0.2.10", "ntp1.example.net"],
        ["ntp1.example.net", "192.0.2.10"],
        ["10.130.60.80/28", "10.130.61.96/28"],
    )
    assert sources == ("192.0.2.10", "ntp1.example.net")
    assert networks == ("10.130.60.80/28", "10.130.61.96/28")


def test_control_plane_ntp_profile_rejects_unapproved_source():
    with pytest.raises(NtpPolicyError, match="NOT_APPROVED"):
        validate_control_plane_ntp_profile(
            ["pool.ntp.org"],
            ["192.0.2.10"],
            ["10.130.60.80/28"],
        )


def test_control_plane_chrony_config_serves_only_worker_networks():
    config = render_control_plane_chrony_config(
        ["192.0.2.10", "ntp1.example.net"],
        ["10.130.60.80/28"],
    )
    assert "server 192.0.2.10 iburst" in config
    assert "server ntp1.example.net iburst" in config
    assert "allow 10.130.60.80/28" in config
    assert "port 123" in config
    assert "cmdport 0" in config
    assert "pool " not in config


def test_control_plane_chrony_bootstrap_installs_and_verifies_udp_123():
    command = render_control_plane_chrony_pre_kubeadm_command(
        ["192.0.2.10"],
        ["10.130.60.80/28"],
    )
    assert "apt-get install -y chrony iproute2" in command
    assert "zypper --non-interactive install chrony iproute2" in command
    assert "chronyc -n sources" in command
    assert "chronyc tracking" in command
    assert "ss -lun" in command
    assert "allow 10.130.60.80/28" in command
    assert "pool " not in command



def test_internal_ntp_probe_requires_synchronized_server_response(monkeypatch):
    class FakeSocket:
        def __init__(self, *_args):
            self.timeout = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, value):
            self.timeout = value

        def sendto(self, _packet, _destination):
            return None

        def recvfrom(self, _size):
            response = bytearray(48)
            response[0] = (4 << 3) | 4
            response[1] = 2
            return bytes(response), ("10.130.60.84", 123)

    monkeypatch.setattr(ntp_policy.socket, "socket", FakeSocket)
    result = ntp_policy.probe_ironic_internal_ntp_endpoint("10.130.60.84")
    assert result["reachable"] is True
    assert result["stratum"] == 2
    assert result["port"] == 123


def test_internal_ntp_probe_rejects_unsynchronized_server(monkeypatch):
    class FakeSocket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _value):
            return None

        def sendto(self, _packet, _destination):
            return None

        def recvfrom(self, _size):
            response = bytearray(48)
            response[0] = (3 << 6) | (4 << 3) | 4
            response[1] = 16
            return bytes(response), ("10.130.60.84", 123)

    monkeypatch.setattr(ntp_policy.socket, "socket", FakeSocket)
    with pytest.raises(NtpPolicyError, match="UNSYNCHRONIZED"):
        ntp_policy.probe_ironic_internal_ntp_endpoint("10.130.60.84")
