"""The network spec carries a version so legacy deployments can be recognized."""

from packages.agent_contracts.infra_networks import (
    NETWORK_SPEC_VERSION,
    comparable_spec,
    derive_network_spec,
    network_spec_migration,
    network_spec_version,
    normalize_link_names,
    plan_infra_networks,
)

PROFILE = {
    "networks": [
        {
            "name": "control",
            "role": "control",
            "host_roles": ["control_plane"],
            "interface": "eno1",
            "vlan": 100,
            "cidr": "10.10.0.0/24",
            "gateway": "10.10.0.1",
            "dhcp": False,
            "mtu": 1500,
            "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}],
        },
        {
            "name": "data",
            "role": "data",
            "host_roles": ["control_plane", "worker", "storage"],
            "interface": "eno2",
            "vlan": 200,
            "cidr": "10.20.0.0/24",
            "gateway": "10.20.0.1",
            "dhcp": False,
            "mtu": 9000,
            "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.20"}],
        },
    ],
    "dns_servers": ["10.10.0.2"],
}


def test_derived_spec_is_stamped_with_the_current_version():
    spec = derive_network_spec(plan_infra_networks(PROFILE))

    assert spec["network_spec_version"] == NETWORK_SPEC_VERSION
    assert network_spec_version(spec) == NETWORK_SPEC_VERSION


def test_unstamped_spec_reads_as_version_one():
    assert network_spec_version({"control_plane_interface": "eno1"}) == 1
    assert network_spec_version(None) == 0


def test_legacy_and_current_specs_compare_equal():
    current = derive_network_spec(plan_infra_networks(PROFILE))
    legacy = {
        "control_plane_interface": "eno1",
        "control_plane_cidr": "10.10.0.0/24",
        "data_plane_interface": "eno2",
        "data_plane_cidr": "10.20.0.0/24",
        "data_plane_mtu": 9000,
        "dns_servers": ["10.10.0.2"],
    }

    assert current["control_plane_interface"] != legacy["control_plane_interface"]
    assert comparable_spec(current) == comparable_spec(legacy)


def test_link_normalization_leaves_cidrs_untouched():
    value = {
        "link": "eno1.100",
        "cidr": "10.10.0.0/24",
        "list": ["bond_control.3220", "eth0"],
    }

    assert normalize_link_names(value) == {
        "link": "eno1",
        "cidr": "10.10.0.0/24",
        "list": ["bond_control", "eth0"],
    }


def test_legacy_spec_gets_a_migration_hint():
    hint = network_spec_migration({"network_spec": {"control_plane_interface": "eno1"}})

    assert hint["code"] == "NETWORK_SPEC_LEGACY"
    assert hint["version"] == 1
    assert hint["current_version"] == NETWORK_SPEC_VERSION


def test_current_spec_needs_no_migration():
    spec = derive_network_spec(plan_infra_networks(PROFILE))

    assert network_spec_migration({"network_spec": spec}) is None
