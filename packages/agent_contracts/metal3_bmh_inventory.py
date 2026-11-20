"""Parse Metal3 BareMetalHost documents into discovery inventory evidence.

The parser is the single contract used by the EPH agent (``metal3.host.inspect``)
and by the platform when it ingests the inspection result. Both sides must agree
on field names and on the fail-closed rules below, so the document is never
interpreted twice with different semantics.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

MAX_HOSTS = 256
MAX_NICS_PER_HOST = 64


class BmhInventoryError(ValueError):
    """Raised when a BareMetalHost document cannot be trusted."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _items(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError as exc:
            raise BmhInventoryError("BMH_DOCUMENT_INVALID") from exc
    if isinstance(document, dict):
        items = document.get("items")
    elif isinstance(document, list):
        items = document
    else:
        items = None
    if not isinstance(items, list):
        raise BmhInventoryError("BMH_DOCUMENT_INVALID")
    if len(items) > MAX_HOSTS:
        raise BmhInventoryError("BMH_HOST_COUNT_EXCEEDED")
    for item in items:
        if not isinstance(item, dict):
            raise BmhInventoryError("BMH_ITEM_INVALID")
    return items


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _bmc_address(spec: dict[str, Any]) -> str:
    bmc = _mapping(spec.get("bmc"))
    address = _text(bmc.get("address")).strip()
    if not address:
        return ""
    parsed = urlparse(address)
    if parsed.scheme:
        return address
    # Bare Metal Host accepts a bare host name or IP; keep it usable as an URL.
    return "https://" + address


def bmc_host_key(address: str) -> str:
    """Return the normalized host part of a BMC endpoint for matching."""

    text = (address or "").strip().lower()
    if not text:
        return ""
    parsed = urlparse(text if "//" in text else "//" + text)
    return (parsed.hostname or "").strip("[]")


def _nics(hardware: dict[str, Any], inventory: dict[str, Any]) -> list[dict[str, Any]]:
    entries = hardware.get("nics")
    if not isinstance(entries, list):
        entries = inventory.get("nics")
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries[:MAX_NICS_PER_HOST]:
        if not isinstance(entry, dict):
            raise BmhInventoryError("BMH_NIC_INVALID")
        nic: dict[str, Any] = {}
        name = _text(entry.get("name")).strip()
        if name:
            nic["name"] = name
        for field in ("mac", "model", "vendor"):
            value = _text(entry.get(field)).strip()
            if value:
                nic[field] = value
        speed = entry.get("speedGbps")
        if isinstance(speed, (int, float)) and not isinstance(speed, bool):
            nic["speed_gbps"] = speed
        pxe = entry.get("pxe")
        if isinstance(pxe, bool):
            nic["pxe"] = pxe
        nic["pci_address"] = _text(entry.get("pciAddress")).strip()
        if name:
            result.append(nic)
    return result


def _storage(hardware: dict[str, Any]) -> list[dict[str, Any]]:
    entries = hardware.get("storage")
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries[:32]:
        if not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {}
        for field in ("name", "model", "serialNumber", "wwn"):
            value = _text(entry.get(field)).strip()
            if value:
                row[field] = value
        size = _integer(entry.get("sizeBytes"))
        if size is not None:
            row["size_bytes"] = size
        if row:
            result.append(row)
    return result


def _host(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(item.get("metadata"))
    spec = _mapping(item.get("spec"))
    status = _mapping(item.get("status"))
    hardware = _mapping(status.get("hardware"))
    inventory = _mapping(status.get("hardware", {}).get("inventory"))
    bmh_name = _text(metadata.get("name")).strip()
    if not bmh_name:
        raise BmhInventoryError("BMH_NAME_REQUIRED")
    nics = _nics(hardware, inventory)
    host = {
        "bmh_name": bmh_name,
        "namespace": _text(metadata.get("namespace")).strip(),
        "bmc_address": _bmc_address(spec),
        "bmc_host": bmc_host_key(_bmc_address(spec)),
        "boot_mac": _text(spec.get("bootMACAddress")).strip().lower(),
        "system_uuid": _text(hardware.get("systemUUID") or spec.get("uuid")).strip(),
        "serial_number": _text(hardware.get("systemSerialNumber")).strip(),
        "manufacturer": _text(hardware.get("manufacturer") or hardware.get("systemVendor")).strip(),
        "product_name": _text(hardware.get("productName")).strip(),
        "power_state": _text(hardware.get("powerState")).strip(),
        "provisioning_state": _text(status.get("provisioning", {}).get("state")).strip()
        if isinstance(status.get("provisioning"), dict)
        else "",
        "operational_status": _text(status.get("operationalStatus")).strip(),
        "error_type": _text(status.get("errorType")).strip(),
        "nics": nics,
        "storage": _storage(hardware),
        "pci_addresses": sorted(
            {nic["pci_address"] for nic in nics if nic.get("pci_address")}
        ),
    }
    memory = _integer(hardware.get("ramMib"))
    if memory is not None:
        host["memory_mib"] = memory
    arch = _text(hardware.get("cpu").get("arch")).strip() if isinstance(
        hardware.get("cpu"), dict
    ) else ""
    if arch:
        host["cpu_arch"] = arch
    return host


def inspect_bmh_document(document: Any) -> dict[str, Any]:
    """Normalize a ``kubectl get bmh -A -o json`` payload."""

    items = _items(document)
    hosts = [_host(item) for item in items]
    names = {(host["namespace"], host["bmh_name"]) for host in hosts}
    if len(names) != len(hosts):
        raise BmhInventoryError("BMH_HOST_DUPLICATE")
    warnings: list[str] = []
    if not hosts:
        warnings.append("BMH_NO_HOSTS")
    if any(host["nics"] for host in hosts) and not any(
        host["pci_addresses"] for host in hosts
    ):
        # Standard BMH hardware carries NIC names/MAC/model/speed only; PCI BDF
        # must come from the IPA inventory or from the OS probe.
        warnings.append("BMH_PCI_ADDRESS_UNAVAILABLE")
    missing_mac = [host["bmh_name"] for host in hosts if not host["boot_mac"]]
    if missing_mac:
        warnings.append("BMH_BOOT_MAC_UNAVAILABLE")
    return {
        "host_count": len(hosts),
        "hosts": hosts,
        "warnings": warnings,
    }


def normalize_hosts(hosts: Any) -> list[dict[str, Any]]:
    """Validate an already parsed ``hosts`` list from a command result."""

    if not isinstance(hosts, list):
        raise BmhInventoryError("BMH_HOSTS_INVALID")
    if len(hosts) > MAX_HOSTS:
        raise BmhInventoryError("BMH_HOST_COUNT_EXCEEDED")
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for host in hosts:
        if not isinstance(host, dict):
            raise BmhInventoryError("BMH_ITEM_INVALID")
        name = _text(host.get("bmh_name")).strip()
        if not name:
            raise BmhInventoryError("BMH_NAME_REQUIRED")
        namespace = _text(host.get("namespace")).strip()
        key = (namespace, name)
        if key in seen:
            raise BmhInventoryError("BMH_HOST_DUPLICATE")
        seen.add(key)
        nics = host.get("nics")
        if nics is not None and not isinstance(nics, list):
            raise BmhInventoryError("BMH_NIC_INVALID")
        result.append(host)
    return result
