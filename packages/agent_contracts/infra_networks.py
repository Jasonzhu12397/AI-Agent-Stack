"""Infrastructure network profile contract shared by platform and agents.

The profile is the single source of truth behind three consumers:

* Metal3 cloud-init ``networkData`` for control-plane and worker hosts;
* the Target Agent worker network contract (control/data separation);
* the Rook-Ceph storage addon (internal/external storage networks).

A network is a VLAN-scoped L2 segment with an optional bond, one IPv4 CIDR,
gateway and Metal3 IP pool. Roles decide which hosts receive the segment in
their cloud-init network data. Storage, control and data CIDRs must never
overlap, and no interface may be silently reused across conflicting roles.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

#: Segment and bond names may carry underscores (``bond_control``,
#: ``sdi_semc``); Kubernetes object names are derived from them by ``dns_name``.
_NAME = re.compile(r"^[a-z0-9](?:[-_a-z0-9]*[a-z0-9])?$")
#: Some hosts name NICs ``pci@0000:3b:00.0``; the kernel accepts that, so do we.
_INTERFACE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
_PCI = re.compile(
    r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$"
)
_BOND_MODE = frozenset({"active-backup", "802.3ad", "balance-rr", "balance-xor"})
_ROLES = frozenset(
    {"control", "data", "storage-internal", "storage-external", "sriov", "provisioning"}
)
_HOST_ROLES = frozenset({"control_plane", "worker", "storage"})
_MAX_NETWORKS = 64
_MAX_IP_POOLS = 8
#: Bumped when the derived network spec changes shape. Specs without the marker
#: predate VLAN sub-interfaces and name the physical interface only.
NETWORK_SPEC_VERSION = 2


class InfraNetworkError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _name(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _NAME.fullmatch(value):
        raise InfraNetworkError(code)
    return value


def dns_name(name: str) -> str:
    """Return the Kubernetes-safe name derived from a segment name.

    IPPool names, ``ipAddressFromIPPool`` references and SR-IOV policy names all
    have to be DNS-1123 names, while the operator writes segment names such as
    ``sdi_semc``. The mapping is deterministic: lower case, underscores become
    hyphens and anything else outside ``[a-z0-9-]`` is dropped.
    """

    text = re.sub(r"[^a-z0-9-]", "-", (name or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        raise InfraNetworkError("INFRA_NETWORK_NAME_INVALID")
    if len(text) > 63:
        text = text[:63].rstrip("-")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", text):
        raise InfraNetworkError("INFRA_NETWORK_NAME_INVALID")
    return text


def _interface(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _INTERFACE.fullmatch(value):
        raise InfraNetworkError(code)
    return value


def _cidr(value: Any, code: str) -> ipaddress.IPv4Network:
    if not isinstance(value, str) or not value:
        raise InfraNetworkError(code)
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise InfraNetworkError(code) from exc
    if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen < 8:
        raise InfraNetworkError(code)
    return network


def _ip_pools(value: Any, network: ipaddress.IPv4Network, code: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_IP_POOLS:
        raise InfraNetworkError(code)
    pools = []
    for item in value:
        if not isinstance(item, dict):
            raise InfraNetworkError(code)
        try:
            start = ipaddress.ip_address(item.get("start"))
            end = ipaddress.ip_address(item.get("end"))
        except (TypeError, ValueError) as exc:
            raise InfraNetworkError(code) from exc
        if not isinstance(start, ipaddress.IPv4Address) or not isinstance(
            end, ipaddress.IPv4Address
        ):
            raise InfraNetworkError(code)
        prefix = item.get("prefix", network.prefixlen)
        if (
            not isinstance(prefix, int)
            or isinstance(prefix, bool)
            or not 1 <= prefix <= 32
        ):
            raise InfraNetworkError(code)
        if start > end or start not in network or end not in network:
            raise InfraNetworkError(code)
        if not start.is_private and not end.is_private:
            raise InfraNetworkError(code)
        pools.append({"start": str(start), "end": str(end), "prefix": prefix})
    for index, left in enumerate(pools):
        for right in pools[index + 1 :]:
            if (
                ipaddress.ip_address(left["start"]) <= ipaddress.ip_address(right["end"])
                and ipaddress.ip_address(right["start"]) <= ipaddress.ip_address(left["end"])
            ):
                raise InfraNetworkError(f"{code}_OVERLAP")
    return pools


#: Roles that belong to the data fabric and stack on the data bond by default.
_DATA_FABRIC_ROLES = frozenset(
    {"data", "storage-internal", "storage-external", "sriov"}
)


def vlan_base_link(plan: dict[str, Any], network: dict[str, Any]) -> str:
    """Return the link a VLAN stacks on, chosen by what the segment carries.

    Control and provisioning ride the management path — the Live ISO, the
    PXE/provisioning VLAN and the control planes all use it — so an undeclared
    VLAN base there is the control link. Storage and SR-IOV segments belong to
    the data fabric and stack on the data bond. A segment that declares its own
    ``bond`` or ``interface`` always wins over this default.
    """

    preferred = (
        ("data", "control")
        if network["role"] in _DATA_FABRIC_ROLES
        else ("control", "data")
    )
    for role in preferred:
        base = network_by_role(plan, role)
        if base is None or base is network or base.get("vlan") is not None:
            continue
        if base.get("bond"):
            return base["bond"]["name"]
        if base.get("interface"):
            return base["interface"]
    raise InfraNetworkError("INFRA_NETWORK_BASE_INTERFACE_REQUIRED")


def network_link_id(plan: dict[str, Any], network: dict[str, Any]) -> str:
    """Return the Metal3/nmstate link id for one network.

    A VLAN that declares neither bond nor interface inherits its base link; the
    link id then follows the same ``<base>.<vlan>`` naming the nmstate renderer
    uses, so both documents describe the same interface.
    """

    if network.get("vlan") is None:
        if network.get("bond"):
            return network["bond"]["name"]
        if network.get("interface"):
            return network["interface"]
        raise InfraNetworkError("INFRA_NETWORK_INTERFACE_INVALID")
    base = (
        network["bond"]["name"]
        if network.get("bond")
        else network.get("interface") or vlan_base_link(plan, network)
    )
    return f"{base}.{network['vlan']}"


def _link_ports(plan: dict[str, Any], link: str) -> list[str]:
    for network in plan["networks"]:
        bond = network.get("bond")
        if bond and bond["name"] == link:
            return list(bond["interfaces"])
        if network.get("interface") == link:
            return [link]
    return []


def pxe_boot_ports(plan: dict[str, Any]) -> list[str]:
    """Return the physical NICs the PXE / Live ISO boot path rides on.

    The provisioning segment (VLAN 70 in the reference lab) is the network the
    machine boots from, and it stacks on the control link alongside the control
    planes. Resolving its base link down to the member ports tells an installer
    which MAC can appear as ``bootMACAddress`` — the bond MAC never can, because
    a bond is assembled by the OS rather than by the firmware PXE client.
    """

    network = network_by_role(plan, "provisioning") or network_by_role(plan, "control")
    if network is None:
        raise InfraNetworkError("INFRA_NETWORK_PXE_LINK_REQUIRED")
    if network.get("bond"):
        base = network["bond"]["name"]
    elif network.get("interface"):
        base = network["interface"]
    elif network.get("vlan") is not None:
        base = vlan_base_link(plan, network)
    else:
        raise InfraNetworkError("INFRA_NETWORK_INTERFACE_INVALID")
    ports = _link_ports(plan, base)
    if not ports:
        raise InfraNetworkError("INFRA_NETWORK_PXE_PORT_REQUIRED")
    return ports


def _network(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InfraNetworkError("INFRA_NETWORK_INVALID")
    name = _name(value.get("name"), "INFRA_NETWORK_NAME_INVALID")
    role = value.get("role")
    if role not in _ROLES:
        raise InfraNetworkError("INFRA_NETWORK_ROLE_INVALID")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InfraNetworkError("INFRA_NETWORK_ENABLED_INVALID")
    vlan = value.get("vlan")
    if vlan is not None and (
        not isinstance(vlan, int) or isinstance(vlan, bool) or not 1 <= vlan <= 4094
    ):
        raise InfraNetworkError("INFRA_NETWORK_VLAN_INVALID")
    mtu = value.get("mtu", 1500)
    if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 9216:
        raise InfraNetworkError("INFRA_NETWORK_MTU_INVALID")
    dhcp = value.get("dhcp", False)
    if not isinstance(dhcp, bool):
        raise InfraNetworkError("INFRA_NETWORK_DHCP_INVALID")
    default_route = value.get("default_route", False)
    if not isinstance(default_route, bool):
        raise InfraNetworkError("INFRA_NETWORK_DEFAULT_ROUTE_INVALID")
    cidr_value = value.get("cidr")
    cidr = None if cidr_value in (None, "") else _cidr(cidr_value, "INFRA_NETWORK_CIDR_INVALID")
    gateway = value.get("gateway_ipv4")
    if gateway in (None, ""):
        gateway = None
    else:
        try:
            gateway_address = ipaddress.ip_address(gateway)
        except (TypeError, ValueError) as exc:
            raise InfraNetworkError("INFRA_NETWORK_GATEWAY_INVALID") from exc
        if not isinstance(gateway_address, ipaddress.IPv4Address):
            raise InfraNetworkError("INFRA_NETWORK_GATEWAY_INVALID")
        if cidr is not None and gateway_address not in cidr:
            raise InfraNetworkError("INFRA_NETWORK_GATEWAY_OUTSIDE_CIDR")
        gateway = str(gateway_address)
    if cidr is None and not dhcp and role != "sriov":
        raise InfraNetworkError("INFRA_NETWORK_CIDR_REQUIRED")
    pools = _ip_pools(value.get("ip_pools"), cidr, "INFRA_NETWORK_IP_POOL_INVALID") if cidr else []
    if cidr is not None and not dhcp and not pools and role != "sriov":
        raise InfraNetworkError("INFRA_NETWORK_IP_POOL_REQUIRED")
    bond = value.get("bond")
    if bond is not None:
        if not isinstance(bond, dict):
            raise InfraNetworkError("INFRA_NETWORK_BOND_INVALID")
        bond_name = _interface(bond.get("name"), "INFRA_NETWORK_BOND_NAME_INVALID")
        interfaces = bond.get("interfaces")
        if (
            not isinstance(interfaces, list)
            or not 1 <= len(interfaces) <= 8
            or len(set(interfaces)) != len(interfaces)
        ):
            raise InfraNetworkError("INFRA_NETWORK_BOND_INTERFACES_INVALID")
        normalized_interfaces = [
            _interface(item, "INFRA_NETWORK_BOND_INTERFACE_INVALID")
            for item in interfaces
        ]
        mode = bond.get("mode", "active-backup")
        if mode not in _BOND_MODE:
            raise InfraNetworkError("INFRA_NETWORK_BOND_MODE_INVALID")
        bond = {
            "name": bond_name,
            "interfaces": normalized_interfaces,
            "mode": mode,
        }
    else:
        bond = None
    direct_interface = None
    if bond is None:
        declared_interface = value.get("interface")
        # A VLAN may inherit its base link at render time (its own data bond or
        # the primary link), so only an untagged network must name one.
        if declared_interface is not None or vlan is None:
            direct_interface = _interface(
                declared_interface, "INFRA_NETWORK_INTERFACE_INVALID"
            )
    pci_addresses = value.get("pci_addresses", [])
    if not isinstance(pci_addresses, list) or len(pci_addresses) > 16:
        raise InfraNetworkError("INFRA_NETWORK_PCI_ADDRESSES_INVALID")
    normalized_pci = []
    for item in pci_addresses:
        if not isinstance(item, str) or not _PCI.fullmatch(item.lower()):
            raise InfraNetworkError("INFRA_NETWORK_PCI_ADDRESS_INVALID")
        normalized_pci.append(item.lower())
    num_vfs = value.get("num_vfs", 8)
    if (
        not isinstance(num_vfs, int)
        or isinstance(num_vfs, bool)
        or not 1 <= num_vfs <= 128
    ):
        raise InfraNetworkError("INFRA_NETWORK_NUM_VFS_INVALID")
    roles = value.get("host_roles")
    if not isinstance(roles, list) or not roles or len(set(roles)) != len(roles):
        raise InfraNetworkError("INFRA_NETWORK_HOST_ROLES_INVALID")
    if any(item not in _HOST_ROLES for item in roles):
        raise InfraNetworkError("INFRA_NETWORK_HOST_ROLES_INVALID")
    return {
        "index": index,
        "name": name,
        "role": role,
        "enabled": enabled,
        "vlan": vlan,
        "mtu": mtu,
        "dhcp": dhcp,
        "default_route": default_route,
        "cidr": str(cidr) if cidr is not None else None,
        "gateway_ipv4": gateway,
        "ip_pools": pools,
        "bond": bond,
        "interface": direct_interface,
        "pci_addresses": normalized_pci,
        "num_vfs": num_vfs,
        "host_roles": list(roles),
    }


def plan_infra_networks(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InfraNetworkError("INFRA_NETWORK_PROFILE_INVALID")
    networks_value = value.get("networks")
    if (
        not isinstance(networks_value, list)
        or not 1 <= len(networks_value) <= _MAX_NETWORKS
    ):
        raise InfraNetworkError("INFRA_NETWORK_PROFILE_NETWORKS_REQUIRED")
    dns = value.get("dns_servers", [])
    if (
        not isinstance(dns, list)
        or len(dns) > 8
        or any(not isinstance(item, str) or not item for item in dns)
    ):
        raise InfraNetworkError("INFRA_NETWORK_DNS_INVALID")
    names: set[str] = set()
    networks = []
    for index, item in enumerate(networks_value):
        network = _network(item, index)
        if not network["enabled"]:
            continue
        if network["name"] in names:
            raise InfraNetworkError("INFRA_NETWORK_NAME_DUPLICATE")
        names.add(network["name"])
        networks.append(network)
    if not networks:
        raise InfraNetworkError("INFRA_NETWORK_PROFILE_EMPTY")
    roles = {network["role"] for network in networks}
    if "control" not in roles:
        raise InfraNetworkError("INFRA_NETWORK_CONTROL_REQUIRED")
    if "data" not in roles:
        raise InfraNetworkError("INFRA_NETWORK_DATA_REQUIRED")
    for role in ("control", "data", "storage-internal", "storage-external"):
        if sum(1 for network in networks if network["role"] == role) > 1:
            raise InfraNetworkError("INFRA_NETWORK_ROLE_DUPLICATE")
    cidr_networks = [
        ipaddress.ip_network(network["cidr"])
        for network in networks
        if network["cidr"] is not None and network["role"] not in {"sriov", "provisioning"}
    ]
    for index, left in enumerate(cidr_networks):
        for right in cidr_networks[index + 1 :]:
            if left.overlaps(right):
                raise InfraNetworkError("INFRA_NETWORK_CIDR_OVERLAP")
    for host_role in _HOST_ROLES:
        if not any(host_role in network["host_roles"] for network in networks):
            raise InfraNetworkError(f"INFRA_NETWORK_{host_role.upper()}_ROLE_UNBOUND")
    if not any(
        network["role"] == "control" and "control_plane" in network["host_roles"]
        for network in networks
    ):
        raise InfraNetworkError("INFRA_NETWORK_CONTROL_PLANE_BINDING_REQUIRED")
    default_networks = [network for network in networks if network["default_route"]]
    if len(default_networks) > 1:
        raise InfraNetworkError("INFRA_NETWORK_DEFAULT_ROUTE_DUPLICATE")
    if default_networks:
        # A default route without a gateway would be an unreachable black hole.
        if not default_networks[0]["gateway_ipv4"]:
            raise InfraNetworkError("INFRA_NETWORK_DEFAULT_ROUTE_GATEWAY_REQUIRED")
        if default_networks[0]["role"] == "sriov":
            raise InfraNetworkError("INFRA_NETWORK_DEFAULT_ROUTE_ROLE_INVALID")
    return {
        "networks": networks,
        "dns_servers": list(dns),
    }


def network_by_role(plan: dict[str, Any], role: str) -> dict[str, Any] | None:
    for network in plan["networks"]:
        if network["role"] == role:
            return network
    return None


def role_links(plan: dict[str, Any], host_role: str) -> list[dict[str, Any]]:
    """Return Metal3 ``networkData.links`` for a host role."""

    ethernets: list[dict[str, Any]] = []
    bonds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for network in plan["networks"]:
        if host_role not in network["host_roles"]:
            continue
        bond = network.get("bond")
        if bond:
            if bond["name"] in seen:
                continue
            for interface in bond["interfaces"]:
                if interface not in seen:
                    ethernets.append({"type": "phy", "id": interface})
                    seen.add(interface)
            bonds.append(
                {
                    "type": "bond",
                    "id": bond["name"],
                    "bondMode": bond["mode"],
                    "interfaces": list(bond["interfaces"]),
                }
            )
            seen.add(bond["name"])
        elif network.get("interface") and network["interface"] not in seen:
            ethernets.append({"type": "phy", "id": network["interface"]})
            seen.add(network["interface"])
    vlans: list[dict[str, Any]] = []
    for network in plan["networks"]:
        if host_role not in network["host_roles"] or network.get("vlan") is None:
            continue
        base = (
            network["bond"]["name"]
            if network.get("bond")
            else network.get("interface") or vlan_base_link(plan, network)
        )
        link = network_link_id(plan, network)
        if link in seen:
            continue
        vlans.append(
            {
                "type": "vlan",
                "id": link,
                "vlanId": network["vlan"],
                "links": [base],
            }
        )
        seen.add(link)
    return {
        "ethernets": ethernets,
        **( {"bonds": bonds} if bonds else {}),
        **( {"vlans": vlans} if vlans else {}),
    }


def render_role_network_data(plan: dict[str, Any], host_role: str) -> dict[str, Any]:
    """Render the Metal3DataTemplate data block for one host role."""

    if host_role not in _HOST_ROLES:
        raise InfraNetworkError("INFRA_NETWORK_HOST_ROLE_INVALID")
    links = role_links(plan, host_role)
    declared_default = any(
        network.get("default_route") for network in plan["networks"]
    )
    ipv4: list[dict[str, Any]] = []
    ipv4_dhcp: list[dict[str, Any]] = []
    for network in plan["networks"]:
        if host_role not in network["host_roles"] or network["cidr"] is None:
            continue
        link = network_link_id(plan, network)
        segment = dns_name(network["name"])
        if network["dhcp"]:
            ipv4_dhcp.append({"id": segment, "link": link})
            continue
        address = {
            "id": segment,
            "link": link,
            "ipAddressFromIPPool": segment,
        }
        if network["gateway_ipv4"] is not None:
            # The OS needs explicit routes; Metal3 networkData does not infer
            # them from the IPPool gateway. Only the declared default-route
            # network owns 0.0.0.0/0, exactly as the nmstate document does.
            owns_default = bool(network.get("default_route")) or (
                not declared_default and network["role"] == "control"
            )
            if owns_default:
                address["routes"] = [
                    {
                        "destination": "0.0.0.0/0",
                        "gateway": network["gateway_ipv4"],
                    }
                ]
            elif network["role"] != "control":
                address["routes"] = [
                    {
                        "destination": network["cidr"],
                        "gateway": network["gateway_ipv4"],
                    }
                ]
        ipv4.append(address)
    networks: dict[str, Any] = {}
    if ipv4:
        networks["ipv4"] = ipv4
    if ipv4_dhcp:
        networks["ipv4DHCP"] = ipv4_dhcp
    if not networks:
        raise InfraNetworkError("INFRA_NETWORK_NO_ADDRESSED_SEGMENT")
    data: dict[str, Any] = {
        "links": links,
        "networks": networks,
    }
    if plan["dns_servers"]:
        data["services"] = {"dns": list(plan["dns_servers"])}
    return data


def derive_network_spec(plan: dict[str, Any]) -> dict[str, Any]:
    control = network_by_role(plan, "control")
    data = network_by_role(plan, "data")
    if control is None or data is None:
        raise InfraNetworkError("INFRA_NETWORK_CONTROL_DATA_REQUIRED")
    if control["cidr"] is None or data["cidr"] is None:
        raise InfraNetworkError("INFRA_NETWORK_CONTROL_DATA_CIDR_REQUIRED")
    if network_link_id(plan, control) == network_link_id(plan, data):
        raise InfraNetworkError("INFRA_NETWORK_CONTROL_DATA_INTERFACE_COLLISION")
    spec = {
        "control_plane_interface": network_link_id(plan, control),
        "control_plane_cidr": control["cidr"],
        "data_plane_interface": network_link_id(plan, data),
        "data_plane_cidr": data["cidr"],
        "data_plane_mtu": data["mtu"],
    }
    if plan["dns_servers"]:
        spec["dns_servers"] = list(plan["dns_servers"])
    spec["network_spec_version"] = NETWORK_SPEC_VERSION
    return spec


def network_spec_version(spec: Any) -> int:
    """Return the speculation version stamped on a spec (1 when unstamped)."""

    if not isinstance(spec, dict):
        return 0
    version = spec.get("network_spec_version")
    if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
        return version
    return 1


_VLAN_LINK = re.compile(r"^([A-Za-z][A-Za-z0-9_-]{0,15})\.\d{1,5}$")


def normalize_link_names(value: Any) -> Any:
    """Collapse VLAN sub-interface names back to their physical interface.

    Specs written before VLAN sub-interfaces were modelled record the physical
    interface ("eno1"); specs derived today record the sub-interface
    ("eno1.100"). Both describe the same cabling, so a comparison that wants to
    decide "is this the same plan" has to reduce them to the same key.
    """

    if isinstance(value, dict):
        return {key: normalize_link_names(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_link_names(item) for item in value]
    if isinstance(value, str):
        match = _VLAN_LINK.fullmatch(value)
        if match:
            return match.group(1)
    return value


def comparable_spec(spec: Any) -> Any:
    """A representation-independent view of a deployment spec.

    Derived artifacts (network spec, cloud-init templates, IP pools) are
    functions of the profile, so comparing them after normalizing link names is
    equivalent to comparing the inputs. Version markers are dropped: they
    describe how the spec was written, not what it says.
    """

    if not isinstance(spec, dict):
        return spec
    normalized = normalize_link_names(spec)
    return {
        key: item
        for key, item in normalized.items()
        if key != "network_spec_version"
    }


def network_spec_migration(spec: Any) -> dict[str, Any] | None:
    """Describe a legacy network spec that predates VLAN sub-interface support."""

    if not isinstance(spec, dict):
        return None
    version = network_spec_version(spec.get("network_spec") or spec)
    if version >= NETWORK_SPEC_VERSION:
        return None
    return {
        "code": "NETWORK_SPEC_LEGACY",
        "version": version,
        "current_version": NETWORK_SPEC_VERSION,
        "detail": (
            "This deployment was planned before VLAN sub-interfaces were "
            "modelled. Its network spec still names the physical interface, "
            "which is not what a re-plan would produce. Re-plan the cluster "
            "from the current profile before re-running installation."
        ),
    }


def render_sriov_node_policies(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Render SR-IOV node policies that bind PCI addresses to VF resources."""

    documents = []
    for network in plan["networks"]:
        if network["role"] != "sriov" or not network["pci_addresses"]:
            continue
        documents.append(
            {
                "apiVersion": "sriovnetwork.openshift.io/v1",
                "kind": "SriovNetworkNodePolicy",
                "metadata": {
                    "name": dns_name(network["name"]),
                    "namespace": "sriov-network-operator",
                },
                "spec": {
                    "nodeSelector": {
                        "feature.node.kubernetes.io/network-sriov.capable": "true"
                    },
                    "numVfs": network["num_vfs"],
                    "nicSelector": {"rootDevices": list(network["pci_addresses"]) },
                    "deviceType": "netdevice",
                    "isRdma": False,
                    "resourceName": dns_name(network["name"]),
                },
            }
        )
    return documents


def render_ip_pools(
    plan: dict[str, Any],
    *,
    cluster_name: str,
    namespace: str = "metal3",
) -> list[dict[str, Any]]:
    if not _NAME.fullmatch(str(cluster_name or "")):
        raise InfraNetworkError("INFRA_NETWORK_CLUSTER_NAME_INVALID")
    documents = []
    for network in plan["networks"]:
        pools = network["ip_pools"]
        if not pools:
            continue
        document = {
            "apiVersion": "ipam.metal3.io/v1alpha1",
            "kind": "IPPool",
            "metadata": {"name": dns_name(network["name"]), "namespace": namespace},
            "spec": {
                "clusterName": cluster_name,
                "namePrefix": f"{dns_name(network['name'])}-",
                "pools": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "prefix": item["prefix"],
                    }
                    for item in pools
                ],
            },
        }
        if network["gateway_ipv4"] is not None:
            document["spec"]["gateway"] = network["gateway_ipv4"]
        documents.append(document)
    return documents
