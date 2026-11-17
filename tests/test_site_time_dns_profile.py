import copy
import importlib

import pytest

agent = importlib.import_module("apps.node_agent.agent")


VALID_PROFILE = {
    "version": "v1",
    "chrony_required": True,
    "resolver_backend": "systemd-resolved",
    "apply_to": ["eph_live_os", "target_os"],
    "ntp_servers": ["time.cloudflare.com", "time.google.com"],
    "dns_servers": ["1.1.1.1", "8.8.8.8"],
    "search_domains": ["lab.example"],
    "dns_probe_name": "time.cloudflare.com",
}


def test_time_dns_profile_requires_chrony_and_both_os_lifecycles():
    result = agent.validate_time_dns_profile(VALID_PROFILE)
    assert result["valid"] is True
    assert result["chrony_required"] is True
    assert set(result["apply_to"]) == {"eph_live_os", "target_os"}
    assert result["ntp_servers"] == ["time.cloudflare.com", "time.google.com"]
    assert result["dns_servers"] == ["1.1.1.1", "8.8.8.8"]
    assert len(result["profile_hash"]) == 64


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"chrony_required": False}, "CHRONY_REQUIRED_FOR_SITE_NODES"),
        ({"apply_to": ["target_os"]}, "TIME_DNS_PROFILE_MUST_COVER_EPH_AND_TARGET_OS"),
        (
            {"apply_to": ["eph_live_os", "target_os", "target_os"]},
            "TIME_DNS_PROFILE_MUST_COVER_EPH_AND_TARGET_OS",
        ),
        ({"ntp_servers": []}, "TIME_DNS_NTP_SERVERS_REQUIRED"),
        ({"ntp_servers": ["time.example;touch /tmp/pwned"]}, "INVALID_TIME_DNS_NTP_SERVER"),
        ({"dns_servers": ["dns.example.net"]}, "INVALID_TIME_DNS_RESOLVER"),
        ({"dns_probe_name": "time.example;id"}, "TIME_DNS_PROBE_NAME_REQUIRED"),
        ({"resolver_backend": "unknown"}, "UNSUPPORTED_DNS_RESOLVER_BACKEND"),
    ],
)
def test_time_dns_profile_rejects_incomplete_or_unsafe_values(change, expected):
    profile = copy.deepcopy(VALID_PROFILE)
    profile.update(change)
    with pytest.raises(agent.NodeAgentError, match=expected):
        agent.validate_time_dns_profile(profile)


def test_time_dns_profile_rejects_duplicate_servers():
    profile = copy.deepcopy(VALID_PROFILE)
    profile["ntp_servers"] = ["time.cloudflare.com", "time.cloudflare.com"]
    with pytest.raises(agent.NodeAgentError, match="DUPLICATE_TIME_DNS_NTP_SERVER"):
        agent.validate_time_dns_profile(profile)
