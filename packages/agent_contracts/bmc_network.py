"""Check that every BMC sits on the control network the EPH provisions from.

In this deployment one L2 segment carries the whole out-of-band path: the BMC
(virtual media and Redfish) of every server, the PXE/DHCP hand-out, the IPA ramdisk
and the Live ISO. The EPH reaches a machine's BMC on the control network and then
boots it from the provisioning VLAN on the same link stack, so a BMC that lives in
a different subnet cannot be powered or provisioned without an extra route that
this deployment deliberately does not have.

Nothing here invents a network: the accepted subnets come from the profile, and a
BMC endpoint that is a hostname is reported as unverifiable rather than assumed to
be reachable.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

CONTROL_ROLES = ("provisioning", "control")

CODE_OFF_NETWORK = "BMC_OFF_CONTROL_NETWORK"
CODE_UNVERIFIABLE = "BMC_ADDRESS_UNVERIFIABLE"
CODE_EMULATOR_OFF_NODE = "BMC_EMULATOR_NOT_ON_CONTROL_NODE"
CODE_EMULATOR_PORT = "BMC_EMULATOR_PORT_NOT_PUBLISHED"
CODE_UNAVAILABLE = "BMC_ADDRESS_UNAVAILABLE"


def bmc_endpoint_host(address: Any) -> str:
    """Return the host part of a Redfish/IPMI endpoint.

    ``redfish+https://10.0.0.5/redfish/v1/Systems/1`` and a bare ``10.0.0.5``
    both resolve to ``10.0.0.5``; anything unparsable returns an empty string so
    the caller can report it instead of guessing.
    """

    if not isinstance(address, str) or not address.strip():
        return ""
    value = address.strip()
    if "://" not in value:
        return value.split("/", 1)[0]
    parts = urlsplit(value)
    return parts.hostname or ""


def bmc_endpoint_port(address: Any) -> int | None:
    """Return the port a BMC endpoint is addressed on, when it names one."""

    if not isinstance(address, str) or not address.strip():
        return None
    value = address.strip()
    if "://" not in value:
        return None
    try:
        port = urlsplit(value).port
    except ValueError:
        return None
    return port


def emulated_bmc_ports(services: Any) -> list[int]:
    """Ports the vbmc/sushy containers publish, read from the bootstrap spec."""

    if not isinstance(services, dict):
        return []
    ports: list[int] = []
    for name in ("vbmc", "sushy-tools"):
        service = services.get(name)
        container = service.get("container") if isinstance(service, dict) else None
        entries = container.get("ports") if isinstance(container, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            port = entry.get("containerPort")
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
                ports.append(port)
    return ports


def control_subnets(plan: dict[str, Any]) -> list[ipaddress.IPv4Network]:
    """Return the IPv4 subnets an EPH can reach a BMC on."""

    subnets: list[ipaddress.IPv4Network] = []
    for network in plan.get("networks", []):
        if network.get("role") not in CONTROL_ROLES:
            continue
        cidr = network.get("cidr")
        if not isinstance(cidr, str) or not cidr.strip():
            continue
        try:
            subnet = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            continue
        if subnet.version == 4:
            subnets.append(subnet)
    return subnets


def _in_subnets(address: ipaddress.IPv4Address, subnets: list) -> bool:
    return any(address in subnet for subnet in subnets)


def bmc_network_findings(
    fleet: Any,
    plan: dict[str, Any],
    *,
    emulated_bmc: bool = False,
    node_ip: str | None = None,
    emulated_ports: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Judge every machine's BMC endpoint against the control network.

    The result is JSON-only so the platform can store it in the deployment
    preflight snapshot and the console can render it: one verdict per machine
    plus the warnings that belong to the deployment as a whole.
    """

    subnets = control_subnets(plan)
    machines: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not subnets:
        warnings.append(CODE_UNAVAILABLE)
    for item in fleet if isinstance(fleet, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        host = bmc_endpoint_host(item.get("bmc_address"))
        verdict: dict[str, Any] = {
            "name": name,
            "bmc_host": host,
            "code": "",
            "detail": "",
        }
        if not host:
            verdict["code"] = CODE_UNAVAILABLE
            verdict["detail"] = "the BMC endpoint carries no host"
        elif emulated_bmc:
            # vbmc/sushy run with host networking on the EPH itself, so their
            # endpoint has to be the control address the EPH owns, on a port the
            # container actually publishes. An address with the right host and
            # a port nothing listens on is the same failure one layer later.
            port = bmc_endpoint_port(item.get("bmc_address"))
            if node_ip and host != node_ip:
                verdict["code"] = CODE_EMULATOR_OFF_NODE
                verdict["detail"] = f"the emulator answers on {node_ip}, not {host}"
            elif emulated_ports and port is not None and port not in emulated_ports:
                verdict["code"] = CODE_EMULATOR_PORT
                verdict["detail"] = (
                    f"nothing publishes {host}:{port}; the emulators serve "
                    + ", ".join(str(item) for item in emulated_ports)
                )
        else:
            try:
                address = ipaddress.IPv4Address(host)
            except ValueError:
                verdict["code"] = CODE_UNVERIFIABLE
                verdict["detail"] = "a BMC hostname cannot be checked without DNS"
            else:
                if subnets and not _in_subnets(address, subnets):
                    verdict["code"] = CODE_OFF_NETWORK
                    verdict["detail"] = (
                        f"{host} is outside "
                        + ", ".join(str(subnet) for subnet in subnets)
                    )
        if verdict["code"]:
            warnings.append(f"{name or 'machine'}: {verdict['code']}")
        machines.append(verdict)
    return {
        "subnets": [str(subnet) for subnet in subnets],
        "machines": machines,
        "warnings": warnings,
        "ok": not warnings,
    }
