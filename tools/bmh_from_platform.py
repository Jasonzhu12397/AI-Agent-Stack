#!/usr/bin/env python3
"""Build the BMH inventory straight from the platform database.

The lab should never need a hand-written YAML inventory: the platform already
knows every machine it has discovered, its BMC endpoint, the vault reference
that holds the BMC credentials and the NICs the agent reported. This module
turns ``AgentMachine`` rows into the same :class:`Machine` objects
``bmh_from_inventory`` renders, so the manifests are generated data rather than
operator input.

Data driven by design:

* the BMC address comes from ``AgentMachine.bmc_endpoint``;
* the credentials are resolved through ``BMC_CREDENTIAL_RESOLVER`` - the vault
  reference stays a reference, nothing is invented and nothing is logged;
* the boot MAC comes from the reported ``hardware_inventory`` and only from
  there: an explicit ``boot_mac``/``boot_mac_address`` field, a NIC flagged as
  the boot NIC, or a machine with exactly one NIC. Anything ambiguous is
  reported and skipped instead of guessed.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API_DIR = ROOT / "apps" / "platform-api"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(PLATFORM_API_DIR) not in sys.path:
    sys.path.insert(0, str(PLATFORM_API_DIR))

from tools.bmh_from_inventory import (  # noqa: E402
    InventoryError,
    Machine,
    normalise_bmc_address,
    normalise_mac,
)

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
NIC_BOOT_FLAGS = ("boot", "is_boot", "bootable", "pxe")
DEFAULT_REDFISH_SYSTEMS_PATH = "/redfish/v1/Systems/1"


def slugify_bmh_name(value: str, *, fallback: str = "") -> str:
    """Derive a DNS label from a node name without inventing a different host."""

    candidate = SLUG_PATTERN.sub("-", (value or "").strip().lower()).strip("-")
    if not candidate:
        candidate = SLUG_PATTERN.sub("-", (fallback or "").strip().lower()).strip("-")
    return candidate[:40].strip("-")


def _iter_nics(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("nics", "interfaces", "network_interfaces"):
        value = inventory.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def boot_mac_from_inventory(
    inventory: dict[str, Any],
    preferred_ports: tuple[str, ...] = (),
) -> str | None:
    """Read the boot MAC from reported hardware, never from a guess.

    ``preferred_ports`` names the physical NICs that carry the PXE network
    (derived from the control link). A multi-NIC server reports no boot flag
    until something boots it, so the PXE port list is what makes the answer
    unambiguous — and a boot MAC outside it would simply not boot.
    """

    if not isinstance(inventory, dict):
        return None
    for key in ("boot_mac", "boot_mac_address", "bootMACAddress"):
        value = inventory.get(key)
        if isinstance(value, str) and value.strip():
            return normalise_mac(value)

    nics = _iter_nics(inventory)
    for nic in nics:
        if any(bool(nic.get(flag)) for flag in NIC_BOOT_FLAGS):
            mac = nic.get("mac") or nic.get("mac_address") or nic.get("hw_address")
            if isinstance(mac, str) and mac.strip():
                chosen = normalise_mac(mac)
                if preferred_ports and not _on_preferred_port(nic, preferred_ports):
                    raise ValueError(
                        f"boot NIC {nic.get('name')} is not on the PXE link "
                        f"({', '.join(preferred_ports)})"
                    )
                return chosen

    pxe_macs: list[str] = []
    for nic in nics:
        if not _on_preferred_port(nic, preferred_ports):
            continue
        mac = _nic_mac(nic)
        if mac:
            pxe_macs.append(mac)
    if len(set(pxe_macs)) == 1:
        return pxe_macs[0]
    if preferred_ports and not pxe_macs:
        # The profile names ports the hardware does not have; booting would
        # silently fail later, so refuse now instead of guessing a NIC.
        raise ValueError(
            "no NIC carries the PXE link " + ", ".join(preferred_ports)
        )
    if len(nics) == 1:
        return _nic_mac(nics[0])
    return None


def _nic_name(nic: dict[str, Any]) -> str:
    for key in ("name", "device", "ifname", "interface"):
        value = nic.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pci_forms(nic: dict[str, Any]) -> set[str]:
    """Return the PCI BDF spellings a port name may use for this NIC."""

    value = ""
    for key in ("pciAddress", "pci_address", "pci", "bdf"):
        item = nic.get(key)
        if isinstance(item, str) and item.strip():
            value = item.strip().lower()
            break
    if not value:
        return set()
    bdf = value.split("pci@", 1)[-1].split("/", 1)[0]
    forms = {bdf, f"pci@{bdf}"}
    if "." in bdf:
        forms.add(f"pci@{bdf.split('.', 1)[0]}")
    return forms


def _on_preferred_port(nic: dict[str, Any], ports: tuple[str, ...]) -> bool:
    name = _nic_name(nic)
    if name and name in ports:
        return True
    pci = _pci_forms(nic)
    return bool(pci) and any(port.lower() in pci for port in ports)


def _nic_mac(nic: dict[str, Any]) -> str | None:
    for key in ("mac", "mac_address", "hw_address"):
        value = nic.get(key)
        if isinstance(value, str) and value.strip():
            return normalise_mac(value)
    return None


def _bmc_address(endpoint: str, systems_path: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise InventoryError("bmc endpoint is empty")
    if "/redfish/" in endpoint or "+" in endpoint:
        return normalise_bmc_address(endpoint)
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"https://{endpoint}"
    return normalise_bmc_address(endpoint.rstrip("/") + systems_path)


def _load_django(settings_module: str):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    import django

    django.setup()
    from core.models import AgentMachine
    from django.conf import settings
    from django.utils.module_loading import import_string

    resolver_path = getattr(settings, "BMC_CREDENTIAL_RESOLVER", "")
    if not resolver_path and getattr(settings, "BMC_VAULT_ADDR", ""):
        resolver_path = "core.bmc_credentials.resolve_bmc_credentials"
    resolver = import_string(resolver_path) if resolver_path else None
    return AgentMachine, resolver


def load_platform_machines(
    *,
    project_id: str | None = None,
    settings_module: str = "platform_api.settings",
    systems_path: str = DEFAULT_REDFISH_SYSTEMS_PATH,
    include_unverified: bool = True,
    skipped: list[str] | None = None,
) -> list[Machine]:
    """Turn AgentMachine rows into renderable machines.

    Machines the platform has not finished configuring are reported through
    ``skipped`` instead of silently disappearing, so a missing BMC endpoint or
    credential shows up as a lab warning rather than a short host count.
    """

    if skipped is None:
        skipped = []

    AgentMachine, resolver = _load_django(settings_module)
    queryset = AgentMachine.objects.select_related("agent_node", "tenant", "project")
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    if not include_unverified:
        queryset = queryset.exclude(identity_status=AgentMachine.IdentityStatus.CONFLICT)

    machines: list[Machine] = []
    for machine in queryset.order_by("agent_node__name"):
        node_name = machine.agent_node.name if machine.agent_node else ""
        name = slugify_bmh_name(machine.bmh_name or node_name, fallback=str(machine.id))
        if not name:
            skipped.append(f"{node_name or machine.id}: no BMH name could be derived")
            continue
        if not machine.bmc_endpoint:
            skipped.append(f"{name}: bmc_endpoint is not configured")
            continue
        if not machine.bmc_credential_ref:
            skipped.append(f"{name}: bmc_credential_ref is not configured")
            continue
        if resolver is None:
            skipped.append(f"{name}: BMC_CREDENTIAL_RESOLVER is not configured")
            continue

        try:
            boot_mac = boot_mac_from_inventory(machine.hardware_inventory or {})
        except InventoryError as exc:
            skipped.append(f"{name}: {exc}")
            continue
        if boot_mac is None:
            skipped.append(f"{name}: hardware_inventory has no unambiguous boot NIC")
            continue

        try:
            credential = resolver(
                credential_ref=machine.bmc_credential_ref,
                trust_bundle_ref=machine.bmc_trust_bundle_ref,
                tenant_id=str(machine.tenant_id),
                project_id=str(machine.project_id),
            )
        except Exception as exc:  # noqa: BLE001 - resolver failures must not abort the fleet
            skipped.append(f"{name}: credential resolver failed ({type(exc).__name__})")
            continue
        if not isinstance(credential, dict) or not credential.get("username"):
            skipped.append(f"{name}: resolver returned no BMC username")
            continue

        try:
            address = _bmc_address(machine.bmc_endpoint, systems_path)
        except InventoryError as exc:
            skipped.append(f"{name}: {exc}")
            continue

        machines.append(
            Machine(
                name=name,
                bmc_address=address,
                boot_mac=boot_mac,
                username=str(credential.get("username")),
                password=str(credential.get("password", "")),
                boot_mode="UEFI",
                verify_bmc_ca=bool(machine.bmc_tls_verify),
                online=True,
                root_device_hints={},
                labels={
                    "ai-agent-stack.io/machine-id": str(machine.id),
                    "ai-agent-stack.io/project-id": str(machine.project_id),
                },
            )
        )

    names = [item.name for item in machines]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise InventoryError(f"duplicate BMH name(s) from the platform: {', '.join(duplicates)}")
    macs = [item.boot_mac for item in machines]
    duplicate_macs = sorted({mac for mac in macs if macs.count(mac) > 1})
    if duplicate_macs:
        raise InventoryError(
            f"duplicate boot MAC(s) from the platform: {', '.join(duplicate_macs)}"
        )

    return machines


def summarise_skips(skipped: Iterable[str], limit: int = 20) -> str:
    items = list(skipped)
    if not items:
        return ""
    shown = items[:limit]
    suffix = "" if len(items) <= limit else f" ... and {len(items) - limit} more"
    return "; ".join(shown) + suffix
