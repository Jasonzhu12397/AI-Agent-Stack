"""One renderer for the Ironic workload configuration implied by a network.

Three callers need the same answer to "what does this provisioning network
imply for Ironic": the deployment validator that fills the submitted spec, the
UI that previews the template before submitting, and the warnings surfaced in
``preflight_result``. The UI used to compute this itself in TypeScript, so the
Python and JavaScript rules could disagree about bond names, VLAN links or which
address wins. The mapping now lives here only, and callers receive the same
skeleton plus the list of values the operator still has to supply.

Image URLs are deliberately never defaulted: the operator owns registry and tag
choices, so the template keeps them empty and names them in ``manual_fields``
instead of inventing a mirror.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from packages.agent_contracts.infra_networks import network_link_id, plan_infra_networks
from packages.agent_contracts.ironic_spec import CR_NAMESPACE

#: Values the provisioning network cannot know. The UI renders these as inputs.
MANUAL_FIELDS: tuple[str, ...] = (
    "api_credentials_name",
    "version",
    "images.ironic",
    "images.deploy_ramdisk_downloader",
    "images.keepalived",
)

WARNING_PROVISIONING_NETWORK_MISSING = "IRONIC_TEMPLATE_PROVISIONING_NETWORK_MISSING"
WARNING_INTERFACE_UNAVAILABLE = "IRONIC_TEMPLATE_INTERFACE_UNAVAILABLE"
WARNING_ADDRESS_UNAVAILABLE = "IRONIC_TEMPLATE_ADDRESS_UNAVAILABLE"

# A /8 would take minutes to walk; the first block is always enough here.
_MAX_ADDRESS_SCAN = 4096


def provisioning_network(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single enabled provisioning network of a planned profile."""

    for network in plan.get("networks") or []:
        if not isinstance(network, dict):
            continue
        if network.get("role") == "provisioning" and network.get("enabled", True):
            return network
    return None


def network_interface(plan: dict[str, Any], network: dict[str, Any]) -> str:
    """Return the link a network is addressed on.

    Delegates to the shared resolver, so a VLAN provisioning network binds the
    ``<base>.<vlan>`` sub-interface the nmstate renderer and Metal3 networkData
    also name, instead of the bare bond the profile inherits from.
    """

    return network_link_id(plan, network)


def provisioning_networking(
    plan: dict[str, Any],
    network: dict[str, Any] | None,
    *,
    eph_address: str | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return ``networking`` keys, their sources, and blocking warnings."""

    networking: dict[str, Any] = {}
    sources: list[str] = []
    warnings: list[str] = []
    if network is None:
        return networking, sources, [WARNING_PROVISIONING_NETWORK_MISSING]
    interface = network_interface(plan, network)
    if interface:
        networking["interface"] = interface
        sources.append("interface")
    else:
        warnings.append(WARNING_INTERFACE_UNAVAILABLE)
    # Only an address the EPH actually owns may be pre-filled, or one derived
    # from the provisioning subnet itself. The gateway belongs to the router on
    # that subnet, so handing it to ironic would put the router's IP on the EPH
    # NIC. A DHCP-addressed provisioning link needs no static address at all.
    address = (eph_address or "").split("/", 1)[0].strip()
    if address:
        networking["ip_address"] = address
        sources.append("ip_address")
    else:
        derived = provisioning_address_from_subnet(network)
        if derived:
            networking["ip_address"] = derived
            sources.append("ip_address_subnet")
        elif network.get("dhcp"):
            networking["dhcp"] = True
        else:
            warnings.append(WARNING_ADDRESS_UNAVAILABLE)
    return networking, sources, warnings


def provisioning_address_from_subnet(network: dict[str, Any]) -> str:
    """Return the first free host of the provisioning subnet, if one exists.

    The address this returns is deterministic: the first usable host that is
    neither the subnet gateway nor inside a declared allocation pool, so the
    ironic provisioning link never collides with the router or with a bare
    metal host lease.
    """

    cidr = network.get("cidr")
    if not isinstance(cidr, str) or not cidr.strip():
        return ""
    if network.get("dhcp"):
        return ""
    try:
        subnet = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        return ""
    if subnet.version != 4:
        return ""
    gateway = network.get("gateway_ipv4")
    gateway_ip = None
    if isinstance(gateway, str) and gateway.strip():
        try:
            gateway_ip = ipaddress.ip_address(gateway.strip())
        except ValueError:
            gateway_ip = None
    pools: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]] = []
    for pool in network.get("ip_pools") or []:
        if not isinstance(pool, dict):
            continue
        try:
            start = ipaddress.ip_address(str(pool.get("start")).strip())
            end = ipaddress.ip_address(str(pool.get("end")).strip())
        except ValueError:
            continue
        if start.version != 4 or end.version != 4:
            continue
        pools.append((start, end))
    hosts = subnet.hosts()
    # The first host of a segment is conventionally the router, declared or not.
    first_host = next(hosts, None)
    for index, host in enumerate(hosts):
        if index >= _MAX_ADDRESS_SCAN:
            break
        if first_host is not None and host == first_host:
            continue
        if gateway_ip is not None and host == gateway_ip:
            continue
        if any(start <= host <= end for start, end in pools):
            continue
        return f"{host}/{subnet.prefixlen}"
    return ""


def ironic_template(
    plan: dict[str, Any],
    *,
    eph_address: str | None = None,
) -> dict[str, Any]:
    """Return the Ironic configuration skeleton plus what the operator must fill."""

    network = provisioning_network(plan)
    networking, sources, warnings = provisioning_networking(
        plan,
        network,
        eph_address=eph_address,
    )
    return {
        "configuration": {
            "namespace": CR_NAMESPACE,
            "api_credentials_name": "",
            "version": "",
            "images": {
                "ironic": "",
                "deploy_ramdisk_downloader": "",
                "keepalived": "",
            },
            "networking": networking,
        },
        "provisioning_network": network.get("name") if network else None,
        "defaults_sources": sources,
        "manual_fields": list(MANUAL_FIELDS),
        "warnings": warnings,
    }


def ironic_template_for_profile(
    profile: Any,
    *,
    eph_address: str | None = None,
) -> dict[str, Any]:
    """Plan a raw profile and render its Ironic template."""

    return ironic_template(plan_infra_networks(profile), eph_address=eph_address)


__all__ = [
    "MANUAL_FIELDS",
    "WARNING_ADDRESS_UNAVAILABLE",
    "WARNING_INTERFACE_UNAVAILABLE",
    "WARNING_PROVISIONING_NETWORK_MISSING",
    "ironic_template",
    "ironic_template_for_profile",
    "network_interface",
    "provisioning_network",
    "provisioning_networking",
]
