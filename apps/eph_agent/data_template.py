"""Derive Metal3DataTemplate content from the declared physical network.

A target cluster cannot be automated honestly with an empty networkData object.
When the operator does not supply a complete data template, this module derives
one from the same network specification that drives the CNI/data-plane plan.
The physical control-plane interface is mandatory; no interface, address or DNS
server is guessed.
"""

from __future__ import annotations

import re
from typing import Any

from packages.agent_contracts.infra_networks import (
    plan_infra_networks,
    render_role_network_data,
)

_INTERFACE = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")


class DataTemplateError(ValueError):
    pass


def default_data_template(network_spec: Any) -> dict[str, Any]:
    if not isinstance(network_spec, dict):
        raise DataTemplateError("METAL3_NETWORK_SPEC_REQUIRED")
    interface = network_spec.get("control_plane_interface")
    if not isinstance(interface, str) or not _INTERFACE.fullmatch(interface):
        raise DataTemplateError("METAL3_CONTROL_PLANE_INTERFACE_REQUIRED")
    dns = network_spec.get("dns_servers", [])
    if (
        not isinstance(dns, list)
        or len(dns) > 8
        or any(not isinstance(server, str) or not server for server in dns)
    ):
        raise DataTemplateError("METAL3_DNS_SERVERS_INVALID")
    # The control-plane link id is what the nmstate renderer and the platform
    # data template name, so <base>.<vlan> or a bond cannot be described as a
    # single physical port. Guessing the base ports would put the node on the
    # wrong L2 segment, so this fallback refuses and the caller must pass the
    # platform-rendered template instead.
    if "." in interface or interface.startswith("bond"):
        raise DataTemplateError("METAL3_DATA_TEMPLATE_REQUIRED")
    network_data: dict[str, Any] = {
        "links": {"ethernets": [{"type": "phy", "id": interface}]},
        "networks": {"ipv4DHCP": [{"id": "control-plane", "link": interface}]},
    }
    if dns:
        network_data["services"] = {"dns": dns}
    return {
        "meta_data": {
            "objectNames": [
                {"key": "machine_name", "object": "machine"},
                {"key": "bmh_name", "object": "baremetalhost"},
            ]
        },
        "network_data": network_data,
    }


META_DATA_OBJECT_NAMES = [
    {"key": "machine_name", "object": "machine"},
    {"key": "bmh_name", "object": "baremetalhost"},
]


def profile_data_template(profile: Any, host_role: str = "control_plane") -> dict[str, Any]:
    """Render the data template from the declared network profile.

    The profile still carries bonds, VLANs and pools, so this path describes the
    real link stack. ``default_data_template`` only knows the link id and is kept
    for payloads that never declared a profile.
    """

    plan = plan_infra_networks(profile)
    return {
        "meta_data": {"objectNames": [dict(item) for item in META_DATA_OBJECT_NAMES]},
        "network_data": render_role_network_data(plan, host_role),
    }
