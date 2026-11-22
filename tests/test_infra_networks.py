from __future__ import annotations

import pytest

from packages.agent_contracts import infra_networks as infra
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
    # The addressed link is the VLAN sub-interface, not the bare bond, so the
    # OS contract names the interface that actually holds the address.
    assert (
        derive_network_spec(plan)["control_plane_interface"] == "bond_control.3220"
    )
    assert derive_network_spec(plan)["data_plane_interface"] == "bond_data.3300"


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
    profile["networks"][0]["vlan"] = None
    profile["networks"][1]["vlan"] = None
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


def _pxe_profile() -> dict:
    """The lab shape: PXE/provisioning VLAN on bond_control, data on bond_data."""
    return {
        "networks": [
            {
                "name": "ctrl",
                "role": "control",
                "mtu": 1500,
                "cidr": "172.18.0.0/24",
                "gateway_ipv4": "172.18.0.1",
                "ip_pools": [{"start": "172.18.0.10", "end": "172.18.0.50"}],
                "bond": {
                    "name": "bond_control",
                    "interfaces": [
                        "pci@0000:18:00.0",
                        "pci@0000:18:00.1",
                    ],
                    "mode": "802.3ad",
                },
                "host_roles": ["control_plane", "worker"],
            },
            {
                # The PXE/provisioning VLAN is declared without a base on
                # purpose: it must stack on the control bond, not the data one.
                "name": "sdi_semc",
                "role": "provisioning",
                "vlan": 70,
                "cidr": "172.18.37.0/24",
                "gateway_ipv4": "172.18.37.1",
                "ip_pools": [{"start": "172.18.37.10", "end": "172.18.37.50"}],
                "host_roles": ["control_plane", "worker"],
            },
            {
                "name": "data_net",
                "role": "data",
                "mtu": 9000,
                "cidr": "10.0.200.0/24",
                "ip_pools": [{"start": "10.0.200.10", "end": "10.0.200.50"}],
                "bond": {
                    "name": "bond_data",
                    "interfaces": [
                        "pci@0000:3b:00.0",
                        "pci@0000:3b:00.1",
                    ],
                    "mode": "802.3ad",
                },
                "host_roles": ["worker", "storage"],
            },
            {
                "name": "ccd_int",
                "role": "storage-internal",
                "vlan": 3220,
                "mtu": 9000,
                "cidr": "10.0.10.0/24",
                "ip_pools": [{"start": "10.0.10.10", "end": "10.0.10.50"}],
                "host_roles": ["worker", "storage"],
            },
        ]
    }


def test_the_pxe_provisioning_vlan_stacks_on_the_control_bond():
    plan = plan_infra_networks(_pxe_profile())
    data = render_role_network_data(plan, "worker")

    vlans = {item["id"]: item for item in data["links"]["vlans"]}
    assert vlans["bond_control.70"]["links"] == ["bond_control"]
    assert vlans["bond_control.70"]["vlanId"] == 70
    # Storage stays on the data fabric.
    assert vlans["bond_data.3220"]["links"] == ["bond_data"]

    addresses = {item["id"]: item for item in data["networks"]["ipv4"]}
    assert addresses["sdi-semc"]["link"] == "bond_control.70"


def test_pci_style_port_names_and_underscored_bonds_are_accepted():
    plan = plan_infra_networks(_pxe_profile())

    control = plan["networks"][0]
    assert control["bond"]["name"] == "bond_control"
    assert control["bond"]["interfaces"] == [
        "pci@0000:18:00.0",
        "pci@0000:18:00.1",
    ]
    assert plan["networks"][1]["name"] == "sdi_semc"


def test_underscored_segment_names_map_to_dns_safe_object_names():
    from packages.agent_contracts.infra_networks import dns_name

    assert dns_name("sdi_semc") == "sdi-semc"
    assert dns_name("ccd_int") == "ccd-int"

    plan = plan_infra_networks(_pxe_profile())
    pools = {item["metadata"]["name"] for item in render_ip_pools(plan, cluster_name="target")}
    assert "sdi-semc" in pools
    assert "sdi_semc" not in pools

def test_pxe_boot_ports_are_the_physical_ports_of_the_control_bond():
    plan = plan_infra_networks(_pxe_profile())
    ports = infra.pxe_boot_ports(plan)
    assert ports == ["pci@0000:18:00.0", "pci@0000:18:00.1"]
    # The bond and the VLAN are assembled in software, so neither can boot.
    assert "bond_control" not in ports
    assert "bond_control.70" not in ports

def test_required_host_links_match_the_rendered_network_data():
    plan = plan_infra_networks(_pxe_profile())
    links = infra.required_host_links(plan)
    assert links == [
        "bond_control",
        "bond_control.70",
        "bond_data",
        "bond_data.3220",
    ]
    # Every checked link is one cloud-init actually brings up.
    data = render_role_network_data(plan, "worker")
    rendered = {item["id"] for key in ("bonds", "vlans") for item in data["links"].get(key, [])}
    assert set(links) == rendered

def test_eph_host_links_skip_segments_the_provisioning_host_never_joins():
    plan = plan_infra_networks(_pxe_profile())
    assert infra.eph_host_links(plan) == [
        "bond_control",
        "bond_control.70",
        "bond_data",
    ]
    # The storage VLAN is on the data bond but is not part of the EPH stack.
    assert "bond_data.3220" not in infra.eph_host_links(plan)


def _host(name: str, **nics) -> dict:
    return {
        "bmh_name": name,
        "nics": [
            {"name": nic_name, "pci_address": pci, "mac": mac}
            for nic_name, pci, mac in nics.get("ports", [])
        ],
    }


def _lab_hosts() -> list[dict]:
    return [
        _host(
            "lab-cp-1",
            ports=[
                ("eno1", "0000:18:00.0", "aa:bb:cc:00:00:01"),
                ("eno2", "0000:18:00.1", "aa:bb:cc:00:00:02"),
                ("ens1f0", "0000:3b:00.0", "aa:bb:cc:00:3b:00"),
                ("ens1f1", "0000:3b:00.1", "aa:bb:cc:00:3b:01"),
                ("ens2f0", "0000:af:00.0", "aa:bb:cc:00:af:00"),
                ("ens2f1", "0000:af:00.1", "aa:bb:cc:00:af:01"),
            ],
        )
    ]


def test_only_physical_ports_are_checked_against_the_inventory():
    """Bonds, bridges and VLAN sub-interfaces are assembled by the OS."""

    plan = plan_infra_networks(_profile())
    ports = infra.declared_physical_ports(plan)
    # A link bound by name but never declared as a bond has no known ports, so
    # nothing can be checked for it - only the ports the profile names count.
    assert ports == ["eno1", "eno2", "ens1f0", "ens1f1"]


def test_every_declared_port_the_inventory_reports_is_matched():
    plan = plan_infra_networks(_profile())
    hosts = _lab_hosts()
    findings = infra.port_inventory_findings(plan, hosts)
    matched = {item["port"]: item["matched"] for item in findings["ports"]}
    assert matched == {
        "eno1": True,
        "eno2": True,
        "ens1f0": True,
        "ens1f1": True,
    }
    assert findings["ok"] is True
    assert findings["warnings"] == []


def test_a_port_no_host_reports_becomes_a_named_warning():
    plan = plan_infra_networks(_profile())
    hosts = _lab_hosts()
    hosts[0]["nics"] = [
        nic for nic in hosts[0]["nics"] if nic["name"] != "ens1f1"
    ]
    findings = infra.port_inventory_findings(plan, hosts)
    assert findings["ok"] is False
    missed = next(item for item in findings["ports"] if item["port"] == "ens1f1")
    assert missed["code"] == infra.CODE_PORT_NOT_COLLECTED
    assert missed["missing_on"] == ["lab-cp-1"]
    assert findings["warnings"][0].startswith(infra.CODE_PORT_NOT_COLLECTED)


def test_each_host_is_judged_on_its_own_inventory():
    """A port one machine reports is not proof the other machine has it."""

    plan = plan_infra_networks(_profile())
    hosts = _lab_hosts()
    second = {
        "bmh_name": "lab-cp-2",
        "nics": [
            {"name": "eno1", "pci_address": "0000:18:00.0", "mac": "aa:bb:cc:00:00:11"},
            {"name": "eno2", "pci_address": "0000:18:00.1", "mac": "aa:bb:cc:00:00:12"},
            {"name": "ens1f0", "pci_address": "0000:3b:00.0", "mac": ""},
            {"name": "ens1f1", "pci_address": "0000:3b:00.1", "mac": ""},
        ],
    }
    hosts.append(second)
    findings = infra.port_inventory_findings(plan, hosts)
    assert findings["ok"] is True
    assert findings["hosts"] == 2


def test_an_empty_inventory_warns_about_nothing_and_claims_nothing():
    plan = plan_infra_networks(_profile())
    findings = infra.port_inventory_findings(plan, [])
    assert findings == {"ok": True, "ports": [], "warnings": [], "hosts": 0}


def test_a_pci_named_port_matches_the_bare_pci_address():
    profile = _profile()
    profile["networks"][0]["bond"]["interfaces"] = ["pci@0000:18:00.0", "eno2"]
    plan = plan_infra_networks(profile)
    findings = infra.port_inventory_findings(plan, _lab_hosts())
    asserted = next(
        item for item in findings["ports"] if item["port"] == "pci@0000:18:00.0"
    )
    assert asserted["matched"] is True
    assert asserted["hosts"] == ["lab-cp-1"]
