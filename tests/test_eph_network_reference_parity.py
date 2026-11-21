"""Parity between the uploaded lab network reference and the rendered nmstate.

The reference document (``bond_control`` untagged control address, ``sdi_semc``
VLAN 70 on the same link, ``bond_data`` 802.3ad with two ports, ``ccd_int``
VLAN 3220, ``ccd_om`` VLAN 3200 carrying the default route) is the shape the
lab actually boots with. This test pins the rendered document to that shape so
a renderer change cannot silently move the default route or the VLAN bases.
"""

from packages.agent_contracts.eph_network import eph_network_state
from packages.agent_contracts.infra_networks import plan_infra_networks


def _reference_profile():
    return {
        "dns_servers": ["10.130.60.1"],
        "networks": [
            {
                "name": "control-net",
                "role": "control",
                "mtu": 1500,
                "cidr": "172.18.0.0/24",
                "ip_pools": [
                    {"start": "172.18.0.3", "end": "172.18.0.10", "prefix": 24}
                ],
                "interface": "bond_control",
                "host_roles": ["control_plane", "worker", "storage"],
            },
            {
                # sdi_semc: SDI agent VLAN 70 stacked directly on bond_control.
                "name": "sdi-semc",
                "role": "provisioning",
                "vlan": 70,
                "cidr": "172.18.37.0/24",
                "gateway_ipv4": "172.18.37.1",
                "ip_pools": [
                    {"start": "172.18.37.3", "end": "172.18.37.10", "prefix": 24}
                ],
                "interface": "bond_control",
                "host_roles": ["control_plane"],
            },
            {
                "name": "data-net",
                "role": "data",
                "mtu": 9000,
                "cidr": "10.0.200.0/24",
                "ip_pools": [
                    {"start": "10.0.200.3", "end": "10.0.200.10", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["enp55s0f0np0", "enp55s0f1np1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["worker", "storage"],
            },
            {
                # ccd_int: internal Kubernetes network.
                "name": "ccd-int",
                "role": "storage-internal",
                "vlan": 3220,
                "mtu": 9000,
                "cidr": "10.0.10.0/24",
                "ip_pools": [
                    {"start": "10.0.10.3", "end": "10.0.10.10", "prefix": 24}
                ],
                "host_roles": ["worker", "storage"],
            },
            {
                # ccd_om: external O&M network that carries the default route.
                "name": "ccd-om",
                "role": "storage-external",
                "vlan": 3200,
                "mtu": 9000,
                "cidr": "10.130.60.80/28",
                "gateway_ipv4": "10.130.60.81",
                "default_route": True,
                "ip_pools": [
                    {"start": "10.130.60.85", "end": "10.130.60.90", "prefix": 28}
                ],
                "host_roles": ["worker", "storage"],
            },
        ],
    }


def _addresses():
    return {
        "control": "172.18.0.3/24",
        "provisioning": "172.18.37.3/24",
        "storage-internal": "10.0.10.3/24",
        "storage-external": "10.130.60.85/28",
    }


def _interfaces(state):
    return {item["name"]: item for item in state["interfaces"]}


def test_reference_shape_renders_untagged_control_and_vlan_base():
    plan = plan_infra_networks(_reference_profile())
    state = eph_network_state(plan, addresses=_addresses())
    interfaces = _interfaces(state)
    control = interfaces["bond_control"]
    assert control["type"] == "ethernet"
    assert control["ipv4"] == {
        "enabled": True,
        "dhcp": False,
        "address": [{"ip": "172.18.0.3", "prefix-length": 24}],
    }
    # As in the reference, the untagged control link owns no default route.
    assert "routes" not in state or all(
        route["next-hop-interface"] != "bond_control"
        for route in state["routes"]["config"]
        if route["destination"] == "0.0.0.0/0"
    )
    sdi = interfaces["bond_control.70"]
    assert sdi["type"] == "vlan"
    assert sdi["vlan"] == {"base-iface": "bond_control", "id": 70}
    assert sdi["ipv4"]["address"] == [{"ip": "172.18.37.3", "prefix-length": 24}]


def test_reference_shape_keeps_the_bond_and_both_storage_vlans():
    plan = plan_infra_networks(_reference_profile())
    state = eph_network_state(plan, addresses=_addresses())
    interfaces = _interfaces(state)
    for port in ("enp55s0f0np0", "enp55s0f1np1"):
        assert interfaces[port]["type"] == "ethernet"
        assert interfaces[port]["mtu"] == 9000
    bond = interfaces["bond_data"]
    assert bond["type"] == "bond"
    assert bond["mtu"] == 9000
    assert bond["link-aggregation"]["mode"] == "802.3ad"
    assert bond["link-aggregation"]["options"] == {
        "miimon": "100",
        "lacp_rate": "1",
        "xmit_hash_policy": "layer3+4",
    }
    assert bond["link-aggregation"]["port"] == [
        {"interface-name": "enp55s0f0np0"},
        {"interface-name": "enp55s0f1np1"},
    ]
    internal = interfaces["bond_data.3220"]
    assert internal["vlan"] == {"base-iface": "bond_data", "id": 3220}
    assert internal["mtu"] == 9000
    assert internal["ipv4"]["address"] == [{"ip": "10.0.10.3", "prefix-length": 24}]
    external = interfaces["bond_data.3200"]
    assert external["vlan"] == {"base-iface": "bond_data", "id": 3200}
    assert external["ipv4"]["address"] == [{"ip": "10.130.60.85", "prefix-length": 28}]


def test_reference_shape_routes_default_via_the_declared_network():
    plan = plan_infra_networks(_reference_profile())
    state = eph_network_state(plan, addresses=_addresses())
    routes = state["routes"]["config"]
    default_routes = [route for route in routes if route["destination"] == "0.0.0.0/0"]
    assert default_routes == [
        {
            "destination": "0.0.0.0/0",
            "next-hop-address": "10.130.60.81",
            "next-hop-interface": "bond_data.3200",
            "table-id": 254,
        }
    ]
    segment_routes = [route for route in routes if route["destination"] == "172.18.37.0/24"]
    assert segment_routes == [
        {
            "destination": "172.18.37.0/24",
            "next-hop-address": "172.18.37.1",
            "next-hop-interface": "bond_control.70",
            "table-id": 254,
        }
    ]
