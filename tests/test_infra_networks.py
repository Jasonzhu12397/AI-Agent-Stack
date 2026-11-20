from __future__ import annotations

import pytest

from packages.agent_contracts.infra_networks import (
    InfraNetworkError,
    derive_network_spec,
    plan_infra_networks,
    render_ip_pools,
    render_role_network_data,
    render_sriov_node_policies,
)


def _profile() -> dict:
    return {
        "dns_servers": ["10.0.60.53", "10.0.61.53"],
        "networks": [
            {
                "name": "ccd-control",
                "role": "control",
                "vlan": 3220,
                "mtu": 1500,
                "cidr": "10.0.60.0/24",
                "gateway_ipv4": "10.0.60.1",
                "ip_pools": [
                    {"start": "10.0.60.10", "end": "10.0.60.250", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["eno1", "eno2"],
                    "mode": "active-backup",
                },
                "pci_addresses": ["0000:5e:00.0"],
                "host_roles": ["control_plane", "worker", "storage"],
            },
            {
                "name": "ccd-data",
                "role": "data",
                "vlan": 3300,
                "mtu": 9000,
                "cidr": "10.0.61.0/24",
                "gateway_ipv4": "10.0.61.1",
                "ip_pools": [
                    {"start": "10.0.61.10", "end": "10.0.61.250", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["ens1f0", "ens1f1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["worker", "storage"],
            },
            {
                "name": "ceph-cluster-net",
                "role": "storage-internal",
                "vlan": 3608,
                "mtu": 9000,
                "cidr": "10.0.70.0/26",
                "gateway_ipv4": "10.0.70.1",
                "ip_pools": [
                    {"start": "10.0.70.2", "end": "10.0.70.62", "prefix": 26}
                ],
                "interface": "bond_storage",
                "host_roles": ["storage", "worker"],
            },
            {
                "name": "ceph-public-net",
                "role": "storage-external",
                "vlan": 3609,
                "mtu": 9000,
                "cidr": "10.0.69.0/24",
                "gateway_ipv4": "10.0.69.1",
                "ip_pools": [
                    {"start": "10.0.69.71", "end": "10.0.69.232", "prefix": 24}
                ],
                "interface": "bond_storage",
                "host_roles": ["storage", "worker"],
            },
            {
                "name": "sriov-data",
                "role": "sriov",
                "vlan": 3400,
                "mtu": 9000,
                "cidr": None,
                "interface": "ens2f0",
                "pci_addresses": ["0000:af:00.1"],
                "host_roles": ["worker"],
            },
        ],
    }


def test_plan_normalizes_network_profile() -> None:
    plan = plan_infra_networks(_profile())
    assert len(plan["networks"]) == 5
    assert derive_network_spec(plan)["control_plane_interface"] == "bond_control"
    assert derive_network_spec(plan)["data_plane_interface"] == "bond_data"


def test_control_plane_cloud_init_uses_bond_and_ip_pool() -> None:
    plan = plan_infra_networks(_profile())
    data = render_role_network_data(plan, "control_plane")
    assert data["links"]["bonds"][0]["id"] == "bond_control"
    assert data["links"]["bonds"][0]["interfaces"] == ["eno1", "eno2"]
    assert data["networks"]["ipv4"][0]["ipAddressFromIPPool"] == "ccd-control"
    assert data["services"]["dns"] == ["10.0.60.53", "10.0.61.53"]
    assert "bond_data" not in [link["id"] for link in data["links"].get("bonds", [])]
    assert data["networks"]["ipv4"][0]["routes"] == [
        {"destination": "0.0.0.0/0", "gateway": "10.0.60.1"}
    ]


def test_worker_cloud_init_carries_data_and_storage_segments() -> None:
    plan = plan_infra_networks(_profile())
    data = render_role_network_data(plan, "worker")
    pools = {item["ipAddressFromIPPool"] for item in data["networks"]["ipv4"]}
    assert pools == {
        "ccd-control",
        "ccd-data",
        "ceph-cluster-net",
        "ceph-public-net",
    }
    bond_ids = {item["id"] for item in data["links"]["bonds"]}
    assert bond_ids == {"bond_control", "bond_data"}
    default_routes = [
        route
        for item in data["networks"]["ipv4"]
        for route in item.get("routes", [])
        if route["destination"] == "0.0.0.0/0"
    ]
    assert default_routes == [{"destination": "0.0.0.0/0", "gateway": "10.0.60.1"}]


def test_ip_pools_render_with_cluster_and_gateway() -> None:
    plan = plan_infra_networks(_profile())
    documents = render_ip_pools(plan, cluster_name="edge-lab")
    assert len(documents) == 4
    control = documents[0]
    assert control["kind"] == "IPPool"
    assert control["metadata"]["name"] == "ccd-control"
    assert control["spec"]["clusterName"] == "edge-lab"
    assert control["spec"]["gateway"] == "10.0.60.1"
    assert control["spec"]["pools"][0]["prefix"] == 24


def test_overlapping_control_and_data_cidr_is_rejected() -> None:
    profile = _profile()
    profile["networks"][1]["cidr"] = "10.0.60.0/25"
    profile["networks"][1]["gateway_ipv4"] = "10.0.60.1"
    profile["networks"][1]["ip_pools"] = [
        {"start": "10.0.60.10", "end": "10.0.60.30", "prefix": 25}
    ]
    with pytest.raises(InfraNetworkError) as exc:
        plan_infra_networks(profile)
    assert str(exc.value) == "INFRA_NETWORK_CIDR_OVERLAP"


def test_control_data_interface_collision_is_rejected() -> None:
    profile = _profile()
    profile["networks"][1]["bond"] = {
        "name": "bond_control",
        "interfaces": ["ens1f0"],
        "mode": "active-backup",
    }
    with pytest.raises(InfraNetworkError) as exc:
        derive_network_spec(plan_infra_networks(profile))
    assert str(exc.value) == "INFRA_NETWORK_CONTROL_DATA_INTERFACE_COLLISION"


def test_storage_role_must_be_bound() -> None:
    profile = _profile()
    for network in profile["networks"]:
        network["host_roles"] = [
            role for role in network["host_roles"] if role != "storage"
        ]
    with pytest.raises(InfraNetworkError) as exc:
        plan_infra_networks(profile)
    assert str(exc.value) == "INFRA_NETWORK_STORAGE_ROLE_UNBOUND"


def test_sriov_policy_binds_pci_addresses_to_vf_resource() -> None:
    plan = plan_infra_networks(_profile())
    policies = render_sriov_node_policies(plan)
    assert len(policies) == 1
    policy = policies[0]
    assert policy["kind"] == "SriovNetworkNodePolicy"
    assert policy["spec"]["nicSelector"]["rootDevices"] == ["0000:af:00.1"]
    assert policy["spec"]["numVfs"] == 8
    assert policy["spec"]["resourceName"] == "sriov-data"
