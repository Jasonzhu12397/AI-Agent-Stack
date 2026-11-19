"""Read the discovered fleet from the platform as planning input.

Separate from :mod:`tools.bmh_from_platform`, which renders manifests for a
single flat inventory: this module keeps the hardware facts the planner needs
(CPU, RAM, disk, identity status, labels) next to the addressing facts (BMC
endpoint, boot MAC, credential reference) and hands back structured specs plus
resolved credentials.

Facts come from the reported ``hardware_inventory`` only. A machine that does
not report CPU or memory is planned on what it does report and flagged through
:meth:`FleetPlan.missing_facts` - the platform never fills those in with a
default.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API_DIR = ROOT / "apps" / "platform-api"
for _path in (ROOT, PLATFORM_API_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from apps.eph_agent.fleet_planning import MachineSpec  # noqa: E402
from tools.bmh_from_platform import (  # noqa: E402
    DEFAULT_REDFISH_SYSTEMS_PATH,
    _bmc_address,
    boot_mac_from_inventory,
    slugify_bmh_name,
)

CPU_KEYS = ("cpu_cores", "cpu_count", "cores", "cpu")
RAM_KEYS = ("ram_mib", "memory_mib", "ram_mebibytes", "memory")
DISK_KEYS = ("disk_gib", "storage_gib", "disk", "storage")


@dataclass(frozen=True)
class PlatformMachine:
    spec: MachineSpec
    username: str
    password: str
    bmc_credential_ref: str


def _first_int(mapping: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def hardware_facts(inventory: dict[str, Any]) -> tuple[int, int, int]:
    """Read CPU cores, RAM MiB and disk GiB from the reported inventory."""

    if not isinstance(inventory, dict):
        return 0, 0, 0
    candidates: list[dict[str, Any]] = [inventory]
    for key in ("hardware", "system", "resources", "cpu_info", "memory_info"):
        nested = inventory.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    cpu = ram = disk = 0
    for candidate in candidates:
        cpu = cpu or _first_int(candidate, CPU_KEYS)
        ram = ram or _first_int(candidate, RAM_KEYS)
        disk = disk or _first_int(candidate, DISK_KEYS)
    return cpu, ram, disk


def load_platform_fleet(
    *,
    project_id: str | None = None,
    settings_module: str = "platform_api.settings",
    systems_path: str = DEFAULT_REDFISH_SYSTEMS_PATH,
    skipped: list[str] | None = None,
) -> list[PlatformMachine]:
    """Return every machine the platform can describe well enough to plan."""

    if skipped is None:
        skipped = []
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

    queryset = AgentMachine.objects.select_related("agent_node", "tenant", "project")
    if project_id:
        queryset = queryset.filter(project_id=project_id)

    fleet: list[PlatformMachine] = []
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
            address = _bmc_address(machine.bmc_endpoint, systems_path)
        except ValueError as exc:
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
        except Exception as exc:  # noqa: BLE001 - one bad reference must not stop the fleet
            skipped.append(f"{name}: credential resolver failed ({type(exc).__name__})")
            continue
        if not isinstance(credential, dict) or not credential.get("username"):
            skipped.append(f"{name}: resolver returned no BMC username")
            continue

        inventory = machine.hardware_inventory or {}
        cpu, ram, disk = hardware_facts(inventory)
        labels = dict(machine.agent_node.labels or {}) if machine.agent_node else {}

        fleet.append(
            PlatformMachine(
                spec=MachineSpec(
                    name=name,
                    machine_id=str(machine.id),
                    project_id=str(machine.project_id),
                    bmc_address=address,
                    boot_mac=boot_mac,
                    secret_ref=f"{name}-bmc",
                    cpu_cores=cpu,
                    ram_mib=ram,
                    disk_gib=disk,
                    labels=labels,
                    identity_status=str(machine.identity_status),
                    online=True,
                    tls_verify=bool(machine.bmc_tls_verify),
                ),
                username=str(credential.get("username")),
                password=str(credential.get("password", "")),
                bmc_credential_ref=machine.bmc_credential_ref,
            )
        )

    names = [item.spec.name for item in fleet]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate BMH name(s) from the platform: {', '.join(duplicates)}")
    macs = [item.spec.boot_mac for item in fleet]
    duplicate_macs = sorted({mac for mac in macs if macs.count(mac) > 1})
    if duplicate_macs:
        raise ValueError(f"duplicate boot MAC(s) from the platform: {', '.join(duplicate_macs)}")
    return fleet
