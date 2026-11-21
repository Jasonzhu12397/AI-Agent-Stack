"""Render the declarative (nmstate) network document for the EPH host.

The EPH node is the machine that runs Ironic, so its own OS networking has to
exist before any control-stack work starts. The project network profile already
describes bonds, VLANs, MTUs and gateways; this module turns that description
into a single nmstate document that the agent applies idempotently.

Host addresses are inputs, never guesses: the profile describes the network,
not which address this EPH owns. Missing addresses fail closed so a wrong
address cannot silently take down the provisioning network. The SR-IOV role is
excluded on purpose - those PCI functions are bound to VF consumers, not to the
EPH management plane.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

BOND_OPTIONS = {
    "miimon": "100",
    "lacp_rate": "1",
    "xmit_hash_policy": "layer3+4",
}
DEFAULT_BOND_MODE = "802.3ad"
ADDRESSED_ROLES = ("provisioning", "control")
_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


class EphNetworkError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise EphNetworkError(code)
    return value


def _parse_cidr(value: str) -> tuple[str, int]:
    try:
        address = ipaddress.IPv4Interface(value)
    except ValueError as exc:
        raise EphNetworkError("EPH_NETWORK_ADDRESS_INVALID") from exc
    return str(address.ip), address.network.prefixlen


def _role(plan: dict[str, Any], role: str) -> dict[str, Any] | None:
    for network in plan["networks"]:
        if network["role"] == role and network.get("enabled", True):
            return network
    return None


def _link(network: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the interface document and the physical port documents."""

    ports = []
    bond = network.get("bond")
    if bond:
        for port in bond["interfaces"]:
            ports.append(
                {
                    "name": port,
                    "type": "ethernet",
                    "state": "up",
                    "mtu": network["mtu"],
                }
            )
        document = {
            "name": bond["name"],
            "type": "bond",
            "state": "up",
            "mtu": network["mtu"],
            "link-aggregation": {
                "mode": bond["mode"],
                "options": dict(BOND_OPTIONS),
                "port": [{"interface-name": port} for port in bond["interfaces"]],
            },
        }
        return document, ports
    name = network.get("interface")
    if not name:
        raise EphNetworkError("EPH_NETWORK_INTERFACE_REQUIRED")
    return (
        {"name": name, "type": "ethernet", "state": "up", "mtu": network["mtu"]},
        [],
    )


def eph_network_state(
    plan: dict[str, Any],
    *,
    addresses: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the nmstate document for the EPH host.

    ``addresses`` maps a network role to ``ip/prefix``. The primary role (the
    control network, falling back to provisioning) always needs one unless the
    profile says DHCP. Other addressed segments are optional.

    Link naming, VLAN parents and default-route ownership come from
    ``infra_networks`` (``network_link_id`` / ``vlan_base_link``) instead of a
    second local implementation, so the nmstate document and the Metal3
    ``networkData`` document cannot describe different interfaces.
    """

    from packages.agent_contracts.infra_networks import (
        network_link_id,
        vlan_base_link,
    )

    if not isinstance(plan, dict) or not isinstance(plan.get("networks"), list):
        raise EphNetworkError("EPH_NETWORK_PROFILE_INVALID")
    if addresses is None:
        addresses = {}
    if not isinstance(addresses, dict):
        raise EphNetworkError("EPH_NETWORK_ADDRESS_INVALID")
    known_roles = {network["role"] for network in plan["networks"]}
    for role in addresses:
        if role not in known_roles:
            raise EphNetworkError("EPH_NETWORK_ADDRESS_ROLE_INVALID")

    control = _role(plan, "control")
    provisioning = _role(plan, "provisioning")
    primary = control or provisioning
    if primary is None:
        raise EphNetworkError("EPH_NETWORK_PRIMARY_ROLE_REQUIRED")

    declared_default = next(
        (
            network
            for network in plan["networks"]
            if network.get("default_route") and network.get("enabled", True)
        ),
        None,
    )
    default_role = declared_default["role"] if declared_default else primary["role"]

    interfaces: list[dict[str, Any]] = []
    seen: set[str] = set()
    routes: list[dict[str, Any]] = []

    def add(document: dict[str, Any]) -> bool:
        if document["name"] in seen:
            return False
        interfaces.append(document)
        seen.add(document["name"])
        return True

    def bind_link(network: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        """Register the link documents for one network and return its link."""

        if network.get("vlan") is None:
            document, ports = _link(network)
            for port in ports:
                add(port)
            return document["name"], document, add(document)
        if network.get("bond") or network.get("interface"):
            base_document, ports = _link(network)
            for port in ports:
                add(port)
            add(base_document)
            base_name = base_document["name"]
        else:
            # A VLAN without its own link stacks on the data bond or the
            # primary link; both must already be registered.
            base_name = vlan_base_link(plan, network)
            if base_name not in seen:
                raise EphNetworkError("EPH_NETWORK_BASE_INTERFACE_REQUIRED")
        document = {
            "name": network_link_id(plan, network),
            "type": "vlan",
            "state": "up",
            "mtu": network["mtu"],
            "vlan": {"base-iface": base_name, "id": network["vlan"]},
        }
        return document["name"], document, add(document)

    def add_default_route(network: dict[str, Any], link: str) -> None:
        gateway = network.get("gateway_ipv4")
        if gateway and network["role"] == default_role:
            routes.append(
                {
                    "destination": "0.0.0.0/0",
                    "next-hop-address": gateway,
                    "next-hop-interface": link,
                    "table-id": 254,
                }
            )

    primary_link, primary_document, _ = bind_link(primary)
    primary_address = addresses.get(primary["role"])
    if primary.get("dhcp"):
        primary_document["ipv4"] = {"enabled": True, "dhcp": True}
    elif primary_address:
        ip, prefix = _parse_cidr(primary_address)
        primary_document["ipv4"] = {
            "enabled": True,
            "dhcp": False,
            "address": [{"ip": ip, "prefix-length": prefix}],
        }
        add_default_route(primary, primary_link)
    else:
        raise EphNetworkError(
            f"EPH_NETWORK_ADDRESS_REQUIRED:{primary['role']}"
        )
    primary_document["ipv6"] = {"enabled": False, "dhcp": False}

    data = _role(plan, "data")
    data_link = ""
    if data is not None:
        data_link, _, _ = bind_link(data)

    for network in plan["networks"]:
        role = network["role"]
        if role in {"control", "data", "sriov"} or network is primary:
            continue
        if not network.get("enabled", True):
            continue
        _check_name(network["name"], "EPH_NETWORK_SEGMENT_NAME_INVALID")
        link, document, created = bind_link(network)
        if not created:
            if network.get("vlan") is None and link == primary_link:
                raise EphNetworkError("EPH_NETWORK_PRIMARY_SEGMENT_CONFLICT")
            raise EphNetworkError("EPH_NETWORK_SEGMENT_NAME_DUPLICATE")
        segment_address = addresses.get(role)
        if role == default_role and not segment_address:
            # A declared default route without an own address cannot be reached.
            raise EphNetworkError("EPH_NETWORK_DEFAULT_ROUTE_ADDRESS_REQUIRED")
        if segment_address:
            ip, prefix = _parse_cidr(segment_address)
            document["ipv4"] = {
                "enabled": True,
                "dhcp": False,
                "address": [{"ip": ip, "prefix-length": prefix}],
            }
            gateway = network.get("gateway_ipv4")
            if gateway and network.get("cidr"):
                routes.append(
                    {
                        "destination": str(network["cidr"]),
                        "next-hop-address": gateway,
                        "next-hop-interface": link,
                        "table-id": 254,
                    }
                )
            add_default_route(network, link)
        else:
            document["ipv4"] = {"enabled": False}
        document["ipv6"] = {"enabled": False, "dhcp": False}

    for document in interfaces:
        if document["type"] in {"bond", "ethernet"}:
            document.setdefault("ipv4", {"enabled": False})
            document.setdefault("ipv6", {"enabled": False, "dhcp": False})
    routes.sort(
        key=lambda route: (
            0 if route["destination"] == "0.0.0.0/0" else 1,
            route["destination"],
            route["next-hop-interface"],
        )
    )
    state: dict[str, Any] = {"interfaces": interfaces}
    if routes:
        state["routes"] = {"config": routes}
    return state


def observed_sriov_pci_defaults(
    profile: dict[str, Any],
    inventories: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Fill SR-IOV PCI addresses from collected host inventory.

    The profile describes intent (which NICs carry VFs); the exact PCI
    functions belong to the hardware and arrive with host inventory. Explicit
    addresses keep winning. Matching prefers the declared interface or bond
    ports; when none is declared the SR-IOV capable functions already reported
    by the hosts are used. Nothing is invented - an empty inventory leaves the
    network empty and produces a warning instead of a fake address.
    """

    import copy

    if not isinstance(profile, dict) or not isinstance(profile.get("networks"), list):
        raise EphNetworkError("EPH_NETWORK_PROFILE_INVALID")
    nic_index: dict[str, dict[str, Any]] = {}
    capable: list[str] = []
    for inventory in inventories:
        if not isinstance(inventory, dict):
            continue
        for nic in inventory.get("nics") or []:
            if not isinstance(nic, dict):
                continue
            name = nic.get("name")
            pci = nic.get("pci_address")
            if not isinstance(pci, str) or not pci:
                continue
            pci = pci.lower()
            if isinstance(name, str) and name:
                nic_index[name] = nic
            if nic.get("sriov_capable") and pci not in capable:
                capable.append(pci)
    updated = copy.deepcopy(profile)
    sources: dict[str, Any] = {}
    warnings: list[str] = []
    for network in updated["networks"]:
        if network.get("role") != "sriov" or network.get("enabled") is False:
            continue
        if network.get("pci_addresses"):
            continue
        wanted: list[str] = []
        interface = network.get("interface")
        if isinstance(interface, str) and interface:
            wanted.append(interface)
        bond = network.get("bond")
        if isinstance(bond, dict):
            for item in bond.get("interfaces") or []:
                if isinstance(item, str) and item:
                    wanted.append(item)
        selected: list[str] = []
        origin = "interface_inventory"
        for name in wanted:
            nic = nic_index.get(name)
            pci = nic.get("pci_address") if nic else None
            if isinstance(pci, str) and pci and pci.lower() not in selected:
                selected.append(pci.lower())
        if not selected and capable:
            selected = list(capable[:16])
            origin = "sriov_capable_inventory"
        name = network.get("name")
        if not selected:
            warnings.append(f"SR-IOV_PCI_UNAVAILABLE:{name}")
            continue
        network["pci_addresses"] = selected
        sources[str(name)] = {
            "origin": origin,
            "interfaces": wanted,
            "pci_addresses": list(selected),
        }
    return updated, sources, warnings
