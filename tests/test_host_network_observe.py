import json
from pathlib import Path

import pytest

from packages.agent_contracts.host_network_observe import (
    HostNetworkObserveError,
    observe_host_networks,
)
from packages.agent_contracts.host_network_probe import collect_local_host_networks

LINKS = [
    {
        "ifname": "eno1",
        "mtu": 1500,
        "address": "aa:bb:cc:dd:ee:01",
        "operstate": "UP",
    },
    {
        "ifname": "eno2",
        "mtu": 1500,
        "address": "aa:bb:cc:dd:ee:02",
        "operstate": "UP",
        "master": "bond0",
    },
    {
        "ifname": "bond0",
        "mtu": 1500,
        "address": "aa:bb:cc:dd:ee:03",
        "operstate": "UP",
        "linkinfo": {"info_kind": "bond", "info_data": {"mode": "active-backup"}},
    },
    {
        "ifname": "eno3",
        "mtu": 9000,
        "address": "aa:bb:cc:dd:ee:04",
        "operstate": "UP",
    },
    {
        "ifname": "eno3.200",
        "link": "eno3",
        "linkinfo": {"info_kind": "vlan", "info_data": {"id": 200}},
    },
]
ADDRS = [
    {"ifname": "eno1", "addr_info": [{"family": "inet"}]},
    {"ifname": "bond0", "addr_info": [{"family": "inet"}]},
    {"ifname": "eno3", "addr_info": [{"family": "inet6"}]},
]


def test_parses_bond_members_vlans_and_families():
    result = observe_host_networks(json.dumps(LINKS), json.dumps(ADDRS))
    by_name = {nic["name"]: nic for nic in result["nics"]}
    assert set(by_name) == {"eno1", "eno2", "bond0", "eno3"}
    assert result["bonds"] == [
        {"name": "bond0", "mode": "active-backup", "interfaces": ["eno2"]}
    ]
    assert by_name["eno3"]["vlans"] == [200]
    assert by_name["eno1"]["address_families"] == ["inet"]
    assert by_name["eno3"]["address_families"] == ["inet6"]


def test_pci_lookup_is_attached():
    result = observe_host_networks(
        LINKS, ADDRS, pci_lookup=lambda name: "0000:3B:00.1" if name == "eno3" else ""
    )
    by_name = {nic["name"]: nic for nic in result["nics"]}
    assert by_name["eno3"]["pci_address"] == "0000:3b:00.1"
    assert "pci_address" not in by_name["eno1"]


def test_invalid_json_is_rejected():
    with pytest.raises(HostNetworkObserveError) as excinfo:
        observe_host_networks("not-json", ADDRS)
    assert excinfo.value.code == "HOST_NETWORK_IP_JSON_INVALID"


class Runner:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def __call__(self, argv):
        if self.fail:
            return {"available": False, "returncode": 127, "stdout": "", "stderr": "missing"}
        payload = LINKS if argv[-1] == "link" else ADDRS
        return {"available": True, "returncode": 0, "stdout": json.dumps(payload)}


def test_collector_uses_sysfs_for_pci(tmp_path: Path):
    (tmp_path / "eno3").mkdir()
    (tmp_path / "eno3" / "device").symlink_to("/sys/devices/pci0000:3b/0000:3b:00.1")
    result = collect_local_host_networks(Runner(), sysfs_root=tmp_path)
    by_name = {nic["name"]: nic for nic in result["nics"]}
    assert by_name["eno3"]["pci_address"] == "0000:3b:00.1"


def test_collector_reads_pci_and_sriov_metadata(tmp_path: Path):
    device = tmp_path / "eno3" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x8086\n", encoding="utf-8")
    (device / "device").write_text("0x1572\n", encoding="utf-8")
    (device / "numa_node").write_text("0\n", encoding="utf-8")
    (device / "sriov_totalvfs").write_text("8\n", encoding="utf-8")
    (device / "sriov_numvfs").write_text("4\n", encoding="utf-8")
    driver = tmp_path / "eno3" / "driver-src"
    driver.mkdir()
    (tmp_path / "eno3" / "device" / "driver").symlink_to(driver)
    (tmp_path / "eno3" / "device" / "pci").symlink_to("/sys/devices/pci0000:3b/0000:3b:00.1")
    # Override the device symlink location used for PCI identification.
    (tmp_path / "eno3" / "device-link").unlink(missing_ok=True)
    result = collect_local_host_networks(Runner(), sysfs_root=tmp_path)
    by_name = {nic["name"]: nic for nic in result["nics"]}
    metadata = by_name["eno3"]
    # The stub sysfs directory is not a PCI symlink, so no fake PCI id is invented.
    assert "pci_address" not in metadata or metadata["pci_address"] == "0000:3b:00.1"
    assert metadata["pci_vendor_id"] == "0x8086"
    assert metadata["pci_device_id"] == "0x1572"
    assert metadata["numa_node"] == 0
    assert metadata["sriov_totalvfs"] == 8
    assert metadata["sriov_numvfs"] == 4


def test_collector_reports_ip_failure(tmp_path: Path):
    with pytest.raises(HostNetworkObserveError) as excinfo:
        collect_local_host_networks(Runner(fail=True), sysfs_root=tmp_path)
    assert excinfo.value.code == "HOST_NETWORK_IP_UNAVAILABLE"
