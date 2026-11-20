
from packages.agent_contracts.host_network_verify import verify_host_networks

PROFILE = {
    "networks": [
        {
            "name": "control",
            "role": "control",
            "vlan": 100,
            "mtu": 1500,
            "cidr": "10.0.0.0/24",
            "gateway_ipv4": "10.0.0.1",
            "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.20"}],
            "bond": {"name": "bond0", "interfaces": ["eno1", "eno2"], "mode": "802.3ad"},
            "host_roles": ["control_plane", "worker"],
        },
        {
            "name": "data",
            "role": "data",
            "vlan": 200,
            "mtu": 9000,
            "cidr": "10.10.0.0/24",
            "gateway_ipv4": "10.10.0.1",
            "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}],
            "interface": "eno3",
            "host_roles": ["control_plane", "worker"],
        },
        {
            "name": "sriov",
            "role": "sriov",
            "vlan": 300,
            "mtu": 9000,
            "interface": "eno5",
            "pci_addresses": ["0000:3b:00.1"],
            "num_vfs": 8,
            "host_roles": ["worker"],
        },
        {
            "name": "storage",
            "role": "storage-internal",
            "vlan": 400,
            "mtu": 9000,
            "cidr": "10.20.0.0/24",
            "gateway_ipv4": "10.20.0.1",
            "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.20"}],
            "interface": "eno4",
            "host_roles": ["storage"],
        },
    ]
}


OBSERVED = {
    "nics": [
        {"name": "eno1", "mtu": 1500, "vlans": [100], "pci_address": "0000:19:00.0"},
        {"name": "eno2", "mtu": 1500, "vlans": [100], "pci_address": "0000:19:00.1"},
        {"name": "eno3", "mtu": 9000, "vlans": [200], "pci_address": "0000:3b:00.0"},
        {"name": "eno5", "mtu": 9000, "vlans": [300], "pci_address": "0000:3b:00.1"},
    ],
    "bonds": [{"name": "bond0", "mode": "802.3ad", "interfaces": ["eno1", "eno2"]}],
}


def test_pass_when_host_and_switch_evidence_match():
    result = verify_host_networks(PROFILE, "control_plane", OBSERVED)
    assert result["status"] == "pass"
    assert result["ok"] is True
    assert result["checked_networks"] == ["control", "data"]


def test_bond_mode_mismatch_fails():
    observed = {
        **OBSERVED,
        "bonds": [{"name": "bond0", "mode": "active-backup", "interfaces": ["eno1", "eno2"]}],
    }
    result = verify_host_networks(PROFILE, "worker", observed)
    assert result["status"] == "fail"
    assert any(item["code"] == "HOST_BOND_MODE_MISMATCH" for item in result["findings"])


def test_missing_interface_fails():
    observed = {"nics": [{"name": "eno1"}], "bonds": []}
    result = verify_host_networks(PROFILE, "control_plane", observed)
    assert result["status"] == "fail"
    assert any(item["code"] == "HOST_INTERFACE_MISSING" for item in result["findings"])


def test_pci_mismatch_fails_for_worker():
    observed = {
        **OBSERVED,
        "nics": [
            {**nic, "pci_address": "0000:3b:00.0"} if nic["name"] == "eno5" else nic
            for nic in OBSERVED["nics"]
        ],
    }
    result = verify_host_networks(PROFILE, "worker", observed)
    assert any(item["code"] == "HOST_PCI_MISMATCH" for item in result["findings"])


def test_missing_pci_evidence_is_unknown():
    observed = {
        "nics": [{"name": "eno1"}, {"name": "eno3"}],
        "bonds": [],
    }
    result = verify_host_networks(PROFILE, "worker", observed)
    assert any(item["code"] == "HOST_PCI_EVIDENCE_MISSING" for item in result["findings"])


def test_storage_role_only_checks_storage_segments():
    result = verify_host_networks(PROFILE, "storage", {"nics": []})
    assert result["checked_networks"] == ["storage"]
