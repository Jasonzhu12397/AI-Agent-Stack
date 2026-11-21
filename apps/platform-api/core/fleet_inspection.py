"""Apply succeeded ``metal3.host.inspect`` commands to the machine inventory.

The EPH reports inspection results through the command lease channel, and the
platform already stores them on the ``AgentCommand`` record. Nothing consumed
them for fleet discovery: only a ClusterDeployment detail read reconciled, so
machines a discovery chain had just inspected stayed empty until someone opened
a deployment.

This module reads those command records directly, applies each result through
the same ``normalize_hosts`` / ``match_machines`` / ``merge_bmh_host`` path the
manual ingest endpoint uses, and records the command id on each machine so a
later pass does not replay the same observation.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.bmh_inventory import match_machines, merge_bmh_host
from core.models import AgentCommand, AgentMachine
from packages.agent_contracts.metal3_bmh_inventory import BmhInventoryError, normalize_hosts

logger = logging.getLogger(__name__)

INSPECT_ACTION = "metal3.host.inspect"
APPLIED_MARKER = "bmh_inspect_command_id"
MAX_COMMANDS_PER_PASS = 20


def apply_inspect_command(command: AgentCommand, machines) -> dict[str, Any]:
    """Merge one succeeded inspection into machines that have not seen it yet."""

    output = command.result.get("output") if isinstance(command.result, dict) else None
    if not isinstance(output, dict):
        return {"command_id": str(command.id), "applied": 0, "reason": "BMH_OUTPUT_MISSING"}
    try:
        hosts = normalize_hosts(output.get("hosts"))
    except BmhInventoryError as exc:
        return {"command_id": str(command.id), "applied": 0, "reason": exc.code}
    pairs, unmatched = match_machines(machines, hosts)
    now = timezone.now()
    applied = 0
    adopted = 0
    with transaction.atomic():
        for host, machine in pairs:
            inventory = machine.hardware_inventory or {}
            if inventory.get(APPLIED_MARKER) == str(command.id):
                continue
            if merge_bmh_host(machine, host, now):
                adopted += 1
            inventory = dict(machine.hardware_inventory or {})
            inventory[APPLIED_MARKER] = str(command.id)
            machine.hardware_inventory = inventory
            machine.save(update_fields=["hardware_inventory", "updated_at"])
            applied += 1
    return {
        "command_id": str(command.id),
        "applied": applied,
        "pci_adopted": adopted,
        "host_count": len(hosts),
        "unmatched": unmatched[:32],
    }


def sync_fleet_inspections(
    tenant,
    project,
    *,
    limit: int = MAX_COMMANDS_PER_PASS,
) -> dict[str, Any]:
    """Apply every unapplied succeeded inspection command for the project."""

    machines = list(
        AgentMachine.objects.filter(tenant=tenant, project=project).select_related("agent_node")
    )
    if not machines:
        return {"commands": [], "applied_machines": 0, "pci_adopted": 0}
    commands = (
        AgentCommand.objects.filter(
            tenant=tenant,
            project=project,
            command_type=INSPECT_ACTION,
            status=AgentCommand.Status.SUCCEEDED,
        )
        .exclude(result=None)
        .order_by("-finished_at", "-updated_at")[:limit]
    )
    reports: list[dict[str, Any]] = []
    applied_machines = 0
    pci_adopted = 0
    for command in commands:
        report = apply_inspect_command(command, machines)
        if report.get("applied"):
            reports.append(report)
            applied_machines += report["applied"]
            pci_adopted += report.get("pci_adopted", 0)
    return {
        "commands": reports,
        "applied_machines": applied_machines,
        "pci_adopted": pci_adopted,
    }


def sync_fleet_inspections_quietly(tenant, project) -> None:
    """Best-effort wrapper for read paths; never fails the request."""

    try:
        sync_fleet_inspections(tenant, project)
    except Exception:  # pragma: no cover - defensive, logged not raised
        logger.warning(
            "fleet inspection sync failed for %s/%s",
            tenant.id,
            project.id,
            exc_info=True,
        )


__all__ = [
    "APPLIED_MARKER",
    "INSPECT_ACTION",
    "apply_inspect_command",
    "sync_fleet_inspections",
    "sync_fleet_inspections_quietly",
]
