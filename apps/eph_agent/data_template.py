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
