"""Parse Linux ``ip -j link`` and ``ip -j addr`` into verification evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class HostNetworkObserveError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _document(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise HostNetworkObserveError("HOST_NETWORK_IP_JSON_INVALID") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HostNetworkObserveError("HOST_NETWORK_IP_JSON_INVALID")
    return value


def _mtu(link: dict[str, Any]) -> int | None:
    value = link.get("mtu")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _kind(link: dict[str, Any]) -> str:
    info = link.get("linkinfo")
    if isinstance(info, dict) and isinstance(info.get("info_kind"), str):
        return info["info_kind"]
    return ""


def _bond_mode(link: dict[str, Any]) -> str:
    info = link.get("linkinfo")
    if not isinstance(info, dict):
        return ""
    data = info.get("info_data")
    if not isinstance(data, dict):
        return ""
    mode = data.get("mode")
    return mode if isinstance(mode, str) else ""


def _vlan_id(link: dict[str, Any]) -> int | None:
    info = link.get("linkinfo")
    if not isinstance(info, dict):
        return None
    data = info.get("info_data")
    if not isinstance(data, dict):
        return None
    value = data.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _addresses(addr_payload: Any) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for item in _document(addr_payload):
        name = item.get("ifname")
        if not isinstance(name, str) or not name:
            continue
        families = []
        for info in item.get("addr_info") or []:
            if not isinstance(info, dict):
                continue
            family = info.get("family")
            if isinstance(family, str) and family:
                families.append(family)
        index[name] = families
    return index


def observe_host_networks(
    link_payload: Any,
    addr_payload: Any,
    *,
    pci_lookup: Callable[[str], str] | None = None,
    metadata_lookup: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build ``nics`` and ``bonds`` entries from real OS link state."""

    links = _document(link_payload)
    families = _addresses(addr_payload)
    nics: list[dict[str, Any]] = []
    bonds: list[dict[str, Any]] = []
    vlan_children: list[tuple[str, int]] = []
    for link in links:
        name = link.get("ifname")
        if not isinstance(name, str) or not name:
            continue
        kind = _kind(link)
        if kind == "vlan":
            vlan = _vlan_id(link)
            parent = link.get("link")
            if vlan is not None and isinstance(parent, str) and parent:
                vlan_children.append((parent, vlan))
            continue
        if kind in {"bridge", "veth", "tun", "dummy"}:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "mac": link.get("address") if isinstance(link.get("address"), str) else "",
            "mtu": _mtu(link),
            "state": link.get("operstate") if isinstance(link.get("operstate"), str) else "",
            "address_families": families.get(name, []),
        }
        pci = pci_lookup(name) if pci_lookup is not None else ""
        if pci:
            entry["pci_address"] = pci.lower()
        if metadata_lookup is not None:
            metadata = metadata_lookup(name)
            if isinstance(metadata, dict):
                for key in (
                    "pci_address",
                    "pci_vendor_id",
                    "pci_device_id",
                    "driver",
                    "numa_node",
                    "sriov_totalvfs",
                    "sriov_numvfs",
                    "sriov_capable",
                ):
                    if key in metadata and metadata[key] not in (None, ""):
                        entry[key] = metadata[key]
        master = link.get("master")
        if kind == "bond" or (isinstance(master, str) and master):
            entry["master"] = master if isinstance(master, str) else ""
        nics.append(entry)
        if kind == "bond":
            bonds.append({"name": name, "mode": _bond_mode(link), "interfaces": []})
    by_name = {bond["name"]: bond for bond in bonds}
    for nic in nics:
        master = nic.get("master")
        if master in by_name:
            by_name[master]["interfaces"].append(nic["name"])
    for parent, vlan in vlan_children:
        for nic in nics:
            if nic["name"] == parent:
                nic.setdefault("vlans", []).append(vlan)
                break
    return {"nics": nics, "bonds": bonds}
