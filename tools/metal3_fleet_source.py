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

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.eph_agent.fleet_planning import MachineSpec  # noqa: E402
from tools.bmc_credentials import resolve_bmc_credentials  # noqa: E402
from tools.bmh_from_platform import (  # noqa: E402
    DEFAULT_REDFISH_SYSTEMS_PATH,
    _bmc_address,
    boot_mac_from_inventory,
    slugify_bmh_name,
)
from tools.platform_state import platform_machines  # noqa: E402

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
    database_url: str | None = None,
    systems_path: str = DEFAULT_REDFISH_SYSTEMS_PATH,
    skipped: list[str] | None = None,
    require_credentials: bool = True,
    pxe_boot_ports: tuple[str, ...] = (),
) -> list[PlatformMachine]:
    """Return every machine the platform can describe well enough to plan."""

    if skipped is None:
        skipped = []
    resolver = resolve_bmc_credentials

    fleet: list[PlatformMachine] = []
    for machine in platform_machines(project_id=project_id, explicit=database_url):
        node_name = machine["agent_node_name"]
        name = slugify_bmh_name(machine.get("bmh_name") or node_name, fallback=str(machine["id"]))
        if not name:
            skipped.append(f"{node_name or machine['id']}: no BMH name could be derived")
            continue
        if not machine.get("bmc_endpoint"):
            skipped.append(f"{name}: bmc_endpoint is not configured")
            continue
        if not machine.get("bmc_credential_ref"):
            skipped.append(f"{name}: bmc_credential_ref is not configured")
            continue

        try:
            boot_mac = boot_mac_from_inventory(
                machine.get("hardware_inventory") or {},
                preferred_ports=pxe_boot_ports,
            )
            address = _bmc_address(machine["bmc_endpoint"], systems_path)
        except ValueError as exc:
            skipped.append(f"{name}: {exc}")
            continue
        if boot_mac is None:
            skipped.append(f"{name}: hardware_inventory has no unambiguous boot NIC")
            continue

        credential: dict[str, Any] = {}
        if require_credentials:
            try:
                credential = resolver(
                    credential_ref=machine["bmc_credential_ref"],
                    trust_bundle_ref=machine.get("bmc_trust_bundle_ref") or "",
                    tenant_id=str(machine["tenant_id"]),
                    project_id=str(machine["project_id"]),
                )
            except Exception as exc:  # noqa: BLE001 - one bad reference must not stop the fleet
                skipped.append(f"{name}: credential resolver failed ({type(exc).__name__})")
                continue
            if not isinstance(credential, dict) or not credential.get("username"):
                skipped.append(f"{name}: resolver returned no BMC username")
                continue

        inventory = machine.get("hardware_inventory") or {}
        cpu, ram, disk = hardware_facts(inventory)
        labels = dict(machine.get("agent_node_labels") or {})

        fleet.append(
            PlatformMachine(
                spec=MachineSpec(
                    name=name,
                    machine_id=str(machine["id"]),
                    project_id=str(machine["project_id"]),
                    bmc_address=address,
                    boot_mac=boot_mac,
                    secret_ref=f"{name}-bmc",
                    cpu_cores=cpu,
                    ram_mib=ram,
                    disk_gib=disk,
                    labels=labels,
                    identity_status=str(machine.get("identity_status")),
                    online=True,
                    tls_verify=bool(machine.get("bmc_tls_verify")),
                ),
                username=str(credential.get("username")),
                password=str(credential.get("password", "")),
                bmc_credential_ref=machine["bmc_credential_ref"],
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
