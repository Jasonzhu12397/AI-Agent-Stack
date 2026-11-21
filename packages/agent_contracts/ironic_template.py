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
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return ``networking`` keys, their sources, and blocking warnings."""

    networking: dict[str, str] = {}
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
    address = (eph_address or "").split("/", 1)[0].strip()
    if not address:
        gateway = network.get("gateway_ipv4")
        address = gateway.strip() if isinstance(gateway, str) else ""
    if address:
        networking["ip_address"] = address
        sources.append("ip_address")
    else:
        warnings.append(WARNING_ADDRESS_UNAVAILABLE)
    return networking, sources, warnings


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
