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


def _pci_for_interface(name: str, sysfs_root: Path) -> str:
    device = sysfs_root / name / "device"
    try:
        target = os.readlink(device)
    except OSError:
        return ""
    return Path(target).name if target else ""


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
        pci_lookup=lambda name: _pci_for_interface(name, sysfs_root),
    )
