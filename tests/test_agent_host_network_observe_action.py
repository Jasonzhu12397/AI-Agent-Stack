import json

import pytest

from apps.eph_agent import main as eph_main
from apps.target_agent import main as target_main

LINKS = [
    {"ifname": "eno1", "mtu": 1500, "address": "aa:bb:cc:dd:ee:01", "operstate": "UP"},
    {"ifname": "eno3", "mtu": 9000, "address": "aa:bb:cc:dd:ee:02", "operstate": "UP"},
]
ADDRS = [{"ifname": "eno1", "addr_info": [{"family": "inet"}]}]


def _runner(argv, timeout=20):
    payload = LINKS if argv[-1] == "link" else ADDRS
    return {"available": True, "returncode": 0, "stdout": json.dumps(payload), "stderr": ""}


@pytest.mark.parametrize("module", [eph_main, target_main])
def test_host_network_observe_action(monkeypatch, module):
    monkeypatch.setattr(module, "_run_read_only", _runner)
    result = module.execute({"action": "host.network.observe"})
    assert result["status"] == "succeeded"
    assert result["side_effects"] is False
    assert result["output"]["nics"][0]["name"] == "eno1"
    assert result["output"]["nics"][1]["name"] == "eno3"


@pytest.mark.parametrize("module", [eph_main, target_main])
def test_host_network_observe_ip_failure(monkeypatch, module):
    monkeypatch.setattr(
        module,
        "_run_read_only",
        lambda argv, timeout=20: {
            "available": False,
            "returncode": 127,
            "stdout": "",
            "stderr": "not found",
        },
    )
    result = module.execute({"action": "host.network.observe"})
    assert result["status"] == "failed"
    assert result["error_code"] == "HOST_NETWORK_IP_UNAVAILABLE"
