"""Collect the local OS network state consumed by host-network verification."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packages.agent_contracts.host_network_observe import (
    HostNetworkObserveError,
    observe_host_networks,
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int | None:
    value = _read(path)
    try:
        return int(value)
    except ValueError:
        return None


def _interface_metadata(name: str, sysfs_root: Path) -> dict[str, Any]:
    interface = sysfs_root / name
    device = interface / "device"
    if not device.exists() and not device.is_symlink():
        return {}
    metadata: dict[str, Any] = {}
    try:
        target = os.readlink(device)
    except OSError:
        target = ""
    if target:
        metadata["pci_address"] = Path(target).name.lower()
    vendor = _read(device / "vendor")
    if vendor:
        metadata["pci_vendor_id"] = vendor.lower()
    device_id = _read(device / "device")
    if device_id:
        metadata["pci_device_id"] = device_id.lower()
    try:
        driver = Path(os.readlink(device / "driver")).name
    except OSError:
        driver = ""
    if driver:
        metadata["driver"] = driver
    numa_node = _read_int(device / "numa_node")
    if numa_node is not None:
        metadata["numa_node"] = numa_node
    total_vfs = _read_int(device / "sriov_totalvfs")
    if total_vfs is not None:
        metadata["sriov_totalvfs"] = total_vfs
        metadata["sriov_capable"] = total_vfs > 0
    num_vfs = _read_int(device / "sriov_numvfs")
    if num_vfs is not None:
        metadata["sriov_numvfs"] = num_vfs
        metadata["sriov_capable"] = num_vfs > 0 or metadata.get("sriov_capable", False)
    return metadata

def collect_local_host_networks(
    runner: Callable[[list[str]], dict[str, Any]],
    *,
    sysfs_root: Path = Path("/sys/class/net"),
) -> dict[str, Any]:
    links = runner(["ip", "-j", "link"])
    addrs = runner(["ip", "-j", "addr"])
    for result in (links, addrs):
        if not isinstance(result, dict) or result.get("available") is not True:
            raise HostNetworkObserveError("HOST_NETWORK_IP_UNAVAILABLE")
        if result.get("returncode") != 0:
            raise HostNetworkObserveError("HOST_NETWORK_IP_FAILED")
    return observe_host_networks(
        links.get("stdout") or "",
        addrs.get("stdout") or "",
        pci_lookup=lambda name: _interface_metadata(name, sysfs_root).get(
            "pci_address", ""
        ),
        metadata_lookup=lambda name: _interface_metadata(name, sysfs_root),
    )
