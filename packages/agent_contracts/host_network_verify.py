"""Host-side OS network declaration verification.

The infrastructure network profile is an intent. This contract compares that
intent with what a host OS actually reports (NICs, bonds, VLANs, MTU and PCI
addresses) and returns explicit findings. Switch-side configuration is out of
scope: only the operating system network data is verified here.
"""

from __future__ import annotations

from typing import Any

from packages.agent_contracts.infra_networks import (
    InfraNetworkError,
    plan_infra_networks,
)

_STATUSES = frozenset({"pass", "fail", "unknown"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _pci(value: Any) -> str:
    return _text(value).lower()


def _nics(observed: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("nics", "network_interfaces", "interfaces"):
        value = observed.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _name(nic: dict[str, Any]) -> str:
    for key in ("name", "interface", "device", "id"):
        value = _text(nic.get(key))
        if value:
            return value
    return ""


def _nic_index(observed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index = {}
    for nic in _nics(observed):
        name = _name(nic)
        if name:
            index[name] = nic
    return index


def _observed_bonds(observed: dict[str, Any]) -> list[dict[str, Any]] | None:
    for key in ("bonds", "bond_interfaces"):
        value = observed.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return None


def _bond_name(bond: dict[str, Any]) -> str:
    for key in ("name", "interface", "id"):
        value = _text(bond.get(key))
        if value:
            return value
    return ""


def _bond_members(bond: dict[str, Any]) -> list[str]:
    for key in ("interfaces", "members", "slaves", "ports"):
        value = bond.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [item.strip() for item in value if item.strip()]
    return []


def _finding(
    code: str,
    status: str,
    *,
    network: str | None = None,
    interface: str | None = None,
    expected: Any = None,
    observed: Any = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": status,
        "network": network,
        "interface": interface,
        "expected": expected,
        "observed": observed,
    }


def _check_bond(
    network: dict[str, Any],
    bond: dict[str, Any],
    observed_bonds: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    name = bond["name"]
    findings: list[dict[str, Any]] = []
    if observed_bonds is None:
        findings.append(
            _finding(
                "HOST_BOND_EVIDENCE_MISSING",
                "unknown",
                network=network["name"],
                interface=name,
                expected={"name": name, "mode": bond["mode"]},
            )
        )
        return findings
    match = next(
        (item for item in observed_bonds if _bond_name(item) == name),
        None,
    )
    if match is None:
        findings.append(
            _finding(
                "HOST_BOND_MISSING",
                "fail",
                network=network["name"],
                interface=name,
            )
        )
        return findings
    mode = _text(match.get("mode") or match.get("bond_mode"))
    if not mode:
        findings.append(
            _finding("HOST_BOND_MODE_UNKNOWN", "unknown", network=network["name"], interface=name)
        )
    elif mode != bond["mode"]:
        findings.append(
            _finding(
                "HOST_BOND_MODE_MISMATCH",
                "fail",
                network=network["name"],
                interface=name,
                expected=bond["mode"],
                observed=mode,
            )
        )
    members = _bond_members(match)
    if not members:
        findings.append(
            _finding(
                "HOST_BOND_MEMBERS_UNKNOWN",
                "unknown",
                network=network["name"],
                interface=name,
            )
        )
    elif sorted(members) != sorted(bond["interfaces"]):
        findings.append(
            _finding(
                "HOST_BOND_MEMBERS_MISMATCH",
                "fail",
                network=network["name"],
                interface=name,
                expected=bond["interfaces"],
                observed=members,
            )
        )
    return findings


def _vlans(nic: dict[str, Any]) -> list[int] | None:
    for key in ("vlans", "vlan_ids"):
        value = nic.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
    vlan = nic.get("vlan")
    if isinstance(vlan, int) and not isinstance(vlan, bool):
        return [vlan]
    return None


def verify_host_networks(
    profile: Any,
    host_role: str,
    observed: Any,
) -> dict[str, Any]:
    if host_role not in {"control_plane", "worker", "storage"}:
        raise InfraNetworkError("INFRA_NETWORK_HOST_ROLE_INVALID")
    if not isinstance(observed, dict):
        raise ValueError("HOST_NETWORK_OBSERVED_INVALID")
    try:
        plan = plan_infra_networks(profile)
    except InfraNetworkError:
        raise
    index = _nic_index(observed)
    observed_bonds = _observed_bonds(observed)
    findings: list[dict[str, Any]] = []
    checked = []
    for network in plan["networks"]:
        if host_role not in network["host_roles"] or not network["enabled"]:
            continue
        checked.append(network["name"])
        bond = network.get("bond")
        link = bond["name"] if bond else network["interface"]
        nic_names = set(index)
        interfaces = bond["interfaces"] if bond else [network["interface"]]
        missing = [item for item in interfaces if item not in nic_names]
        if missing:
            findings.append(
                _finding(
                    "HOST_INTERFACE_MISSING",
                    "fail",
                    network=network["name"],
                    interface=link,
                    expected=interfaces,
                    observed=sorted(nic_names),
                )
            )
        if bond:
            findings.extend(_check_bond(network, bond, observed_bonds))
        for interface in interfaces:
            nic = index.get(interface)
            if nic is None:
                continue
            mtu = nic.get("mtu")
            if not isinstance(mtu, int) or isinstance(mtu, bool):
                findings.append(
                    _finding(
                        "HOST_MTU_UNKNOWN",
                        "unknown",
                        network=network["name"],
                        interface=interface,
                        expected=network["mtu"],
                    )
                )
            elif mtu != network["mtu"]:
                findings.append(
                    _finding(
                        "HOST_MTU_MISMATCH",
                        "fail",
                        network=network["name"],
                        interface=interface,
                        expected=network["mtu"],
                        observed=mtu,
                    )
                )
            vlans = _vlans(nic)
            if network["vlan"] is not None:
                if vlans is None:
                    findings.append(
                        _finding(
                            "HOST_VLAN_UNKNOWN",
                            "unknown",
                            network=network["name"],
                            interface=interface,
                            expected=network["vlan"],
                        )
                    )
                elif network["vlan"] not in vlans:
                    findings.append(
                        _finding(
                            "HOST_VLAN_MISMATCH",
                            "fail",
                            network=network["name"],
                            interface=interface,
                            expected=network["vlan"],
                            observed=vlans,
                        )
                    )
        for pci in network["pci_addresses"]:
            present = any(
                _pci(nic.get("pci_address") or nic.get("pci")) == pci
                for nic in index.values()
            )
            has_pci = any(
                _pci(nic.get("pci_address") or nic.get("pci")) for nic in index.values()
            )
            if not has_pci:
                findings.append(
                    _finding(
                        "HOST_PCI_EVIDENCE_MISSING",
                        "unknown",
                        network=network["name"],
                        interface=link,
                        expected=pci,
                    )
                )
            elif not present:
                findings.append(
                    _finding(
                        "HOST_PCI_MISMATCH",
                        "fail",
                        network=network["name"],
                        interface=link,
                        expected=pci,
                    )
                )
        if network["role"] == "sriov" and network["pci_addresses"]:
            for nic in index.values():
                if _pci(nic.get("pci_address") or nic.get("pci")) not in network[
                    "pci_addresses"
                ]:
                    continue
                total_vfs = nic.get("sriov_totalvfs")
                num_vfs = nic.get("sriov_numvfs")
                if isinstance(total_vfs, int) and not isinstance(total_vfs, bool):
                    if total_vfs < network["num_vfs"]:
                        findings.append(
                            _finding(
                                "HOST_SRIOV_TOTALVFS_INSUFFICIENT",
                                "fail",
                                network=network["name"],
                                interface=_name(nic) or link,
                                expected=network["num_vfs"],
                                observed=total_vfs,
                            )
                        )
                if isinstance(num_vfs, int) and not isinstance(num_vfs, bool):
                    if num_vfs != network["num_vfs"]:
                        findings.append(
                            _finding(
                                "HOST_SRIOV_NUMVFS_MISMATCH",
                                "fail",
                                network=network["name"],
                                interface=_name(nic) or link,
                                expected=network["num_vfs"],
                                observed=num_vfs,
                            )
                        )
                elif not isinstance(total_vfs, int) or isinstance(total_vfs, bool):
                    findings.append(
                        _finding(
                            "HOST_SRIOV_VFS_UNKNOWN",
                            "unknown",
                            network=network["name"],
                            interface=_name(nic) or link,
                            expected=network["num_vfs"],
                        )
                    )
    if not checked:
        findings.append(
            _finding("HOST_ROLE_HAS_NO_NETWORK", "fail", network=None, interface=None)
        )
    statuses = [item["status"] for item in findings]
    if "fail" in statuses:
        status = "fail"
    elif "unknown" in statuses:
        status = "unknown"
    else:
        status = "pass"
    observed_nics = [
        {
            key: nic[key]
            for key in (
                "name",
                "mtu",
                "vlans",
                "pci_address",
                "pci_vendor_id",
                "pci_device_id",
                "driver",
                "numa_node",
                "sriov_totalvfs",
                "sriov_numvfs",
            )
            if key in nic and nic[key] not in (None, "", [])
        }
        for nic in index.values()
    ]
    return {
        "host_role": host_role,
        "status": status,
        "ok": status == "pass",
        "checked_networks": checked,
        "findings": findings,
        "observed_interfaces": sorted(index),
        "observed_nics": observed_nics,
    }
