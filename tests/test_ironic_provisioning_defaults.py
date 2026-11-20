"""Provisioning network values prefill the Ironic workload, explicitly only."""

from types import SimpleNamespace

from core.cluster_deployment_views import _validated_spec

BASE_NETWORKS = [
    {
        "name": "control",
        "role": "control",
        "vlan": 100,
        "mtu": 1500,
        "cidr": "10.0.0.0/24",
        "gateway_ipv4": "10.0.0.1",
        "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.50"}],
        "interface": "eno1",
        "host_roles": ["control_plane", "worker"],
    },
    {
        "name": "data",
        "role": "data",
        "vlan": 200,
        "mtu": 9000,
        "cidr": "10.10.0.0/24",
        "gateway_ipv4": "10.10.0.1",
        "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.50"}],
        "interface": "eno2",
        "host_roles": ["worker"],
    },
    {
        "name": "storage",
        "role": "storage-internal",
        "vlan": 300,
        "mtu": 9000,
        "cidr": "10.20.0.0/24",
        "gateway_ipv4": "10.20.0.1",
        "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.50"}],
        "interface": "eno3",
        "host_roles": ["storage"],
    },
]


def _profile(provisioning=None, **overrides):
    network = {
        "name": "provisioning",
        "role": "provisioning",
        "vlan": 400,
        "mtu": 1500,
        "cidr": "10.40.0.0/24",
        "gateway_ipv4": "10.40.0.1",
        "ip_pools": [{"start": "10.40.0.10", "end": "10.40.0.50"}],
        "interface": "eno4",
        "host_roles": ["worker"],
    }
    network.update(overrides)
    return {"networks": [*BASE_NETWORKS, network if provisioning is None else provisioning]}


def _cluster_spec(ironic_networking=None, version="v1.37.2"):
    ironic = {
        "api_credentials_name": "ironic-auth",
        "version": "37.0",
        "images": {
            "ironic": "registry.local/ironic:37.0",
            "deploy_ramdisk_downloader": "registry.local/ipa-downloader:latest",
            "keepalived": "registry.local/keepalived:2.2.4",
        },
    }
    if ironic_networking is not None:
        ironic["networking"] = ironic_networking
    return {
        "cluster_name": "edge-lab",
        "kubernetes_version": version,
        "control_plane_replicas": 3,
        "worker_replicas": 2,
        "ironic": ironic,
    }


def _validate(spec, profile):
    request = SimpleNamespace(data={"cluster_spec": spec})
    return _validated_spec(request, infra_profile=profile)


def test_provisioning_network_fills_interface_and_address():
    spec, error = _validate(_cluster_spec(), _profile())

    assert error is None
    assert spec["ironic"]["networking"] == {
        "interface": "eno4",
        "ip_address": "10.40.0.1",
    }
    assert spec["ironic_defaults_sources"] == ["interface", "ip_address"]


def test_explicit_values_win_over_the_provisioning_network():
    spec, error = _validate(
        _cluster_spec({"interface": "eno9", "ip_address": "10.40.0.99"}),
        _profile(),
    )

    assert error is None
    assert spec["ironic"]["networking"] == {
        "interface": "eno9",
        "ip_address": "10.40.0.99",
    }
    assert "ironic_defaults_sources" not in spec


def test_bond_name_is_the_provisioning_link():
    profile = _profile(
        {
            "name": "provisioning",
            "role": "provisioning",
            "vlan": 400,
            "cidr": "10.40.0.0/24",
            "gateway_ipv4": "10.40.0.1",
            "ip_pools": [{"start": "10.40.0.10", "end": "10.40.0.50"}],
            "bond": {"name": "bond-prov", "interfaces": ["eno4", "eno5"], "mode": "active-backup"},
            "host_roles": ["worker"],
        }
    )
    spec, error = _validate(_cluster_spec(), profile)

    assert error is None
    assert spec["ironic"]["networking"]["interface"] == "bond-prov"


def test_absent_gateway_still_requires_an_explicit_address():
    profile = _profile(
        {
            "name": "provisioning",
            "role": "provisioning",
            "vlan": 400,
            "cidr": "10.40.0.0/24",
            "gateway_ipv4": None,
            "ip_pools": [{"start": "10.40.0.10", "end": "10.40.0.50"}],
            "interface": "eno4",
            "host_roles": ["worker"],
        }
    )
    spec, error = _validate(_cluster_spec(), profile)

    assert spec is None
    assert error == "ironic: IRONIC_IP_ADDRESS_REQUIRED"


def test_profile_without_provisioning_network_uses_only_explicit_values():
    spec, error = _validate(
        _cluster_spec({"interface": "eno4", "ip_address": "10.40.0.1"}),
        {"networks": BASE_NETWORKS},
    )

    assert error is None
    assert "ironic_defaults_sources" not in spec


def test_untrusted_network_shapes_do_not_crash_the_validator():
    profile = _profile(provisioning="not-a-mapping")
    spec, error = _validate(_cluster_spec(), profile)

    # The profile validator rejects the malformed entry; the Ironic error then
    # surfaces the missing networking values instead of an AttributeError.
    assert spec is None
    assert error is not None


def test_eph_owned_address_wins_over_the_gateway_prefill():
    spec = _cluster_spec()
    spec["eph_network_addresses"] = {
        "control": "10.40.0.20/24",
        "provisioning": "10.40.0.30/24",
    }
    result, error = _validate(spec, _profile())

    assert error is None
    assert result["ironic"]["networking"]["ip_address"] == "10.40.0.30"
    assert result["eph_network_addresses"]["provisioning"] == "10.40.0.30/24"
