"""One control segment carries every BMC and the PXE / Live ISO path.

These tests pin the reachability judgement: a BMC in the control or
provisioning subnet is accepted, a BMC elsewhere is reported instead of
silently trusted, and an emulated BMC has to answer on the EPH's own address.
"""

from __future__ import annotations

from packages.agent_contracts.bmc_network import (
    CODE_EMULATOR_OFF_NODE,
    CODE_OFF_NETWORK,
    CODE_UNAVAILABLE,
    CODE_UNVERIFIABLE,
    bmc_endpoint_host,
    bmc_network_findings,
    control_subnets,
)
from packages.agent_contracts.infra_networks import plan_infra_networks


def _profile() -> dict:
    return {
        "networks": [
            {
                "name": "ctrl",
                "role": "control",
                "cidr": "172.18.0.0/24",
                "ip_pools": [{"start": "172.18.0.10", "end": "172.18.0.50"}],
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["pci@0000:18:00.0", "pci@0000:18:00.1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["control_plane", "worker"],
            },
            {
                "name": "sdi_semc",
                "role": "provisioning",
                "vlan": 70,
                "cidr": "172.18.37.0/24",
                "ip_pools": [{"start": "172.18.37.10", "end": "172.18.37.50"}],
                "host_roles": ["control_plane", "worker"],
            },
            {
                "name": "data_net",
                "role": "data",
                "cidr": "10.0.200.0/24",
                "ip_pools": [{"start": "10.0.200.10", "end": "10.0.200.50"}],
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["pci@0000:3b:00.0", "pci@0000:3b:00.1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["control_plane", "worker", "storage"],
            },
        ]
    }


def test_bmc_host_is_read_from_a_redfish_or_bare_endpoint():
    assert bmc_endpoint_host("redfish+https://10.0.0.5/redfish/v1/Systems/1") == "10.0.0.5"
    assert bmc_endpoint_host("https://bmc-01.example/redfish") == "bmc-01.example"
    assert bmc_endpoint_host("10.0.0.5") == "10.0.0.5"
    assert bmc_endpoint_host("") == ""


def test_control_and_provisioning_subnets_are_the_bmc_fabric():
    plan = plan_infra_networks(_profile())
    assert [str(item) for item in control_subnets(plan)] == [
        "172.18.0.0/24",
        "172.18.37.0/24",
    ]


def test_a_bmc_in_the_control_network_passes():
    plan = plan_infra_networks(_profile())
    result = bmc_network_findings(
        [
            {"name": "cp-1", "bmc_address": "redfish+https://172.18.0.21/redfish/v1/Systems/1"},
            {"name": "cp-2", "bmc_address": "redfish+https://172.18.37.22/redfish/v1/Systems/1"},
        ],
        plan,
    )
    assert result["ok"] is True
    assert result["warnings"] == []
    assert {item["code"] for item in result["machines"]} == {""}


def test_a_bmc_outside_the_control_network_is_reported():
    plan = plan_infra_networks(_profile())
    result = bmc_network_findings(
        [{"name": "cp-1", "bmc_address": "redfish+https://192.168.9.21/redfish/v1/Systems/1"}],
        plan,
    )
    assert result["ok"] is False
    assert result["warnings"] == [f"cp-1: {CODE_OFF_NETWORK}"]
    assert "192.168.9.21 is outside 172.18.0.0/24, 172.18.37.0/24" in (
        result["machines"][0]["detail"]
    )


def test_a_bmc_without_a_usable_host_is_reported():
    plan = plan_infra_networks(_profile())
    result = bmc_network_findings([{"name": "cp-1", "bmc_address": ""}], plan)
    assert result["warnings"] == [f"cp-1: {CODE_UNAVAILABLE}"]

    hostname = bmc_network_findings(
        [{"name": "cp-2", "bmc_address": "https://bmc-01.example/redfish"}], plan
    )
    assert hostname["warnings"] == [f"cp-2: {CODE_UNVERIFIABLE}"]


def test_an_emulated_bmc_must_answer_on_the_eph_address():
    plan = plan_infra_networks(_profile())
    result = bmc_network_findings(
        [{"name": "vm-1", "bmc_address": "redfish+https://172.18.0.99:8000/redfish/v1/Systems/1"}],
        plan,
        emulated_bmc=True,
        node_ip="172.18.0.3",
    )
    assert result["warnings"] == [f"vm-1: {CODE_EMULATOR_OFF_NODE}"]

    matches = bmc_network_findings(
        [{"name": "vm-1", "bmc_address": "redfish+https://172.18.0.3:8000/redfish/v1/Systems/1"}],
        plan,
        emulated_bmc=True,
        node_ip="172.18.0.3",
    )
    assert matches["ok"] is True


def test_a_profile_without_a_control_subnet_blocks_the_judgement():
    plan = plan_infra_networks(_profile())
    kept = [
        item
        for item in plan["networks"]
        if item["role"] not in ("control", "provisioning")
    ]
    plan = {**plan, "networks": kept}
    result = bmc_network_findings([{"name": "cp-1", "bmc_address": "10.0.0.5"}], plan)
    assert CODE_UNAVAILABLE in result["warnings"]
