import pytest

from packages.agent_contracts.eph_network import (
    EphNetworkError,
    eph_network_state,
    observed_sriov_pci_defaults,
)
from packages.agent_contracts.infra_networks import plan_infra_networks


def _profile():
    return {
        "dns_servers": ["10.130.60.1"],
        "networks": [
            {
                "name": "ctrl-net",
                "role": "control",
                "mtu": 1500,
                "cidr": "172.18.0.0/24",
                "ip_pools": [{"start": "172.18.0.10", "end": "172.18.0.50", "prefix": 24}],
                "gateway_ipv4": "172.18.0.1",
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["eno1", "eno2"],
                    "mode": "802.3ad",
                },
                "host_roles": ["control_plane"],
            },
            {
                "name": "prov-net",
                "role": "provisioning",
                "vlan": 70,
                "cidr": "172.18.37.0/24",
                "gateway_ipv4": "172.18.37.1",
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["eno1", "eno2"],
                    "mode": "802.3ad",
                },
                "host_roles": ["control_plane"],
                "ip_pools": [{"start": "172.18.37.10", "end": "172.18.37.50", "prefix": 24}],
            },
            {
                "name": "data-net",
                "role": "data",
                "mtu": 9000,
                "cidr": "10.0.200.0/24",
                "ip_pools": [{"start": "10.0.200.10", "end": "10.0.200.50", "prefix": 24}],
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["enp55s0f0np0", "enp55s0f1np1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["worker"],
            },
            {
                "name": "storage-int",
                "role": "storage-internal",
                "vlan": 3220,
                "mtu": 9000,
                "cidr": "10.0.10.0/24",
                "ip_pools": [{"start": "10.0.10.10", "end": "10.0.10.50", "prefix": 24}],
                "gateway_ipv4": "10.0.10.1",
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["enp55s0f0np0", "enp55s0f1np1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["storage"],
            },
            {
                "name": "storage-ext",
                "role": "storage-external",
                "vlan": 3200,
                "mtu": 9000,
                "cidr": "10.130.60.80/28",
                "ip_pools": [{"start": "10.130.60.90", "end": "10.130.60.94", "prefix": 28}],
                "gateway_ipv4": "10.130.60.81",
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["enp55s0f0np0", "enp55s0f1np1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["storage"],
            },
        ],
    }


def test_eph_network_renders_bond_vlan_and_default_route():
    plan = plan_infra_networks(_profile())
    state = eph_network_state(
        plan,
        addresses={
            "control": "172.18.0.3/24",
            "provisioning": "172.18.37.3/24",
            "storage-internal": "10.0.10.3/24",
            "storage-external": "10.130.60.85/28",
        },
    )
    by_name = {item["name"]: item for item in state["interfaces"]}
    assert by_name["eno1"]["type"] == "ethernet"
    assert by_name["bond_control"]["type"] == "bond"
    assert by_name["bond_control"]["link-aggregation"]["port"] == [
        {"interface-name": "eno1"},
        {"interface-name": "eno2"},
    ]
    assert by_name["bond_data"]["mtu"] == 9000
    assert by_name["bond_control.70"]["type"] == "vlan"
    assert by_name["bond_control.70"]["vlan"] == {"base-iface": "bond_control", "id": 70}
    assert by_name["bond_control.70"]["ipv4"]["address"] == [
        {"ip": "172.18.37.3", "prefix-length": 24}
    ]
    assert by_name["bond_control"]["ipv4"]["address"] == [
        {"ip": "172.18.0.3", "prefix-length": 24}
    ]
    assert by_name["bond_data.3220"]["vlan"]["base-iface"] == "bond_data"
    assert by_name["bond_data.3200"]["ipv4"]["address"] == [
        {"ip": "10.130.60.85", "prefix-length": 28}
    ]
    destinations = [route["destination"] for route in state["routes"]["config"]]
    assert "0.0.0.0/0" in destinations
    assert "10.130.60.80/28" in destinations
    default = next(
        route for route in state["routes"]["config"] if route["destination"] == "0.0.0.0/0"
    )
    assert default["next-hop-interface"] == "bond_control"
    assert default["next-hop-address"] == "172.18.0.1"


def test_eph_network_requires_primary_address():
    plan = plan_infra_networks(_profile())
    with pytest.raises(EphNetworkError) as excinfo:
        eph_network_state(plan, addresses={"storage-internal": "10.0.10.3/24"})
    assert excinfo.value.code == "EPH_NETWORK_ADDRESS_REQUIRED:control"


def test_eph_network_rejects_unknown_role_address():
    plan = plan_infra_networks(_profile())
    with pytest.raises(EphNetworkError) as excinfo:
        eph_network_state(plan, addresses={"control": "172.18.0.3/24", "sriov": "10.0.0.1/24"})
    assert excinfo.value.code == "EPH_NETWORK_ADDRESS_ROLE_INVALID"


def test_eph_network_rejects_invalid_address():
    plan = plan_infra_networks(_profile())
    with pytest.raises(EphNetworkError) as excinfo:
        eph_network_state(plan, addresses={"control": "not-an-ip"})
    assert excinfo.value.code == "EPH_NETWORK_ADDRESS_INVALID"


def test_sriov_pci_defaults_use_declared_interface():
    profile = {
        "networks": [
            {
                "name": "ctrl-net",
                "role": "control",
                "bond": {"name": "bond-control", "interfaces": ["eno1"]},
                "host_roles": ["control_plane"],
            },
            {
                "name": "sriov-dp",
                "role": "sriov",
                "interface": "enp55s0f0np0",
                "num_vfs": 16,
                "host_roles": ["worker"],
            },
        ]
    }
    inventories = [
        {
            "nics": [
                {"name": "eno1", "pci_address": "0000:18:00.1"},
                {
                    "name": "enp55s0f0np0",
                    "pci_address": "0000:3b:00.0",
                    "sriov_capable": True,
                },
            ]
        }
    ]
    updated, sources, warnings = observed_sriov_pci_defaults(profile, inventories)
    network = next(item for item in updated["networks"] if item["role"] == "sriov")
    assert network["pci_addresses"] == ["0000:3b:00.0"]
    assert sources["sriov-dp"]["origin"] == "interface_inventory"
    assert warnings == []


def test_sriov_pci_defaults_fall_back_to_capable_functions():
    profile = {
        "networks": [
            {
                "name": "sriov-dp",
                "role": "sriov",
                "bond": {"name": "bond-dp", "interfaces": ["missing0"]},
                "num_vfs": 16,
                "host_roles": ["worker"],
            }
        ]
    }
    inventories = [
        {
            "nics": [
                {"name": "eno0", "pci_address": "0000:18:00.1"},
                {"name": "eno9", "pci_address": "0000:af:00.1", "sriov_capable": True},
            ]
        }
    ]
    updated, sources, _warnings = observed_sriov_pci_defaults(profile, inventories)
    assert updated["networks"][0]["pci_addresses"] == ["0000:af:00.1"]
    assert sources["sriov-dp"]["origin"] == "sriov_capable_inventory"


def test_sriov_pci_defaults_keep_explicit_addresses():
    profile = {
        "networks": [
            {
                "name": "sriov-dp",
                "role": "sriov",
                "pci_addresses": ["0000:3b:00.1"],
                "num_vfs": 8,
                "host_roles": ["worker"],
            }
        ]
    }
    updated, sources, warnings = observed_sriov_pci_defaults(
        profile, [{"nics": [{"name": "x", "pci_address": "0000:af:00.1"}]}]
    )
    assert updated["networks"][0]["pci_addresses"] == ["0000:3b:00.1"]
    assert sources == {}
    assert warnings == []


def test_sriov_pci_defaults_warn_without_inventory():
    profile = {
        "networks": [
            {
                "name": "sriov-dp",
                "role": "sriov",
                "interface": "enp55s0f0np0",
                "num_vfs": 8,
                "host_roles": ["worker"],
            }
        ]
    }
    updated, sources, warnings = observed_sriov_pci_defaults(profile, [])
    assert updated["networks"][0].get("pci_addresses") in (None, [])
    assert sources == {}
    assert warnings == ["SR-IOV_PCI_UNAVAILABLE:sriov-dp"]
