"""Reconcile a deployment with what the EPH has already reported.

The platform used to require two manual buttons before it knew anything the EPH
had observed: one to pull the runtime preflight verdict, one to pull the
``metal3.host.inspect`` inventory. Both read the same command-result channel that
the deployment detail page already has to consult, so a stale snapshot was the
normal case and the UI had to fall back to guessing from the spec.

This module owns the two pure "payload -> snapshot" transforms and a single
``reconcile_deployment`` entry point that reads both action results and stores
them only when they are newer than what is already recorded. Callers that hold a
client reuse it; the detail view calls the best-effort wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.bmh_inventory import match_machines, merge_bmh_host
from core.cloud_control_bridge import (
    CloudControlBridgeError,
    CloudControlNotConfigured,
    build_cloud_control_client,
)
from core.cloud_control_client import CloudControlDispatchError
from core.models import AgentMachine
from packages.agent_contracts.metal3_bmh_inventory import BmhInventoryError, normalize_hosts

logger = logging.getLogger(__name__)

EPH_PREFLIGHT_ACTION = "eph.preflight"
BMH_INSPECT_ACTION = "metal3.host.inspect"

MAX_WARNINGS = 32
MAX_WARNING_LENGTH = 160

#: Stored snapshot sections written by this module.
EPH_PREFLIGHT_KEY = "eph_preflight"
BMH_INVENTORY_KEY = "bmh_inventory_sync"


def _bounded_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for item in value[:MAX_WARNINGS]:
        text = str(item)[:MAX_WARNING_LENGTH]
        if text and text not in warnings:
            warnings.append(text)
    return warnings


def eph_preflight_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn an ``eph.preflight`` action result into the stored snapshot."""

    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    return {
        "available": bool(payload.get("available")),
        "reason": payload.get("reason"),
        "status": payload.get("status"),
        "error_code": payload.get("error_code"),
        "eph_ready": output.get("eph_ready"),
        "warnings": _bounded_warnings(output.get("warnings")),
        "observed_at": output.get("observed_at"),
        "command_id": payload.get("command_id"),
        "synced_at": timezone.now().isoformat(),
    }


def _comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The snapshot without the field stamped when it was stored."""

    return {key: value for key, value in snapshot.items() if key != "synced_at"}


def _is_stale(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """A snapshot is stale when its content differs from what is stored.

    Comparing only ``command_id`` and ``observed_at`` missed the case the
    bridges actually produce: the same command polled again with a different
    verdict keeps both identifiers, so updated warnings were never written.
    """

    if not isinstance(stored, dict) or not stored:
        return True
    return _comparable(stored) != _comparable(incoming)


def store_snapshot(deployment, key: str, snapshot: dict[str, Any]) -> bool:
    """Write one snapshot section unless the stored one is already current."""

    preflight = (
        dict(deployment.preflight_result)
        if isinstance(deployment.preflight_result, dict)
        else {}
    )
    if not _is_stale(preflight.get(key) or {}, snapshot):
        return False
    preflight[key] = snapshot
    deployment.preflight_result = preflight
    deployment.save(update_fields=["preflight_result", "updated_at"])
    return True


def bmh_inventory_snapshot(
    deployment,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]] | None, str | None]:
    """Turn a ``metal3.host.inspect`` result into a snapshot and machine updates.

    The third element is a fail-closed error code; when it is set the numbers in
    the snapshot are zero and no machine was touched.
    """

    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    record: dict[str, Any] = {
        "available": bool(payload.get("available")),
        "reason": payload.get("reason"),
        "status": payload.get("status"),
        "error_code": payload.get("error_code"),
        "command_id": payload.get("command_id"),
        "host_count": 0,
        "matched": 0,
        "unmatched": [],
        "warnings": [],
        "observed_at": output.get("observed_at"),
        "synced_at": timezone.now().isoformat(),
    }
    if not record["available"]:
        return record, None, None
    if payload.get("status") != "succeeded":
        record["reason"] = "BMH_INSPECT_NOT_SUCCEEDED"
        return record, None, "bmh_inspect_not_succeeded"
    try:
        hosts = normalize_hosts(output.get("hosts"))
    except BmhInventoryError as exc:
        record["reason"] = exc.code
        return record, None, exc.code
    machines = list(
        AgentMachine.objects.filter(
            tenant=deployment.tenant,
            project=deployment.project,
        ).select_related("agent_node")
    )
    pairs, unmatched = match_machines(machines, hosts)
    now = timezone.now()
    updated: list[dict[str, Any]] = []
    with transaction.atomic():
        for host, machine in pairs:
            adopted_pci = merge_bmh_host(machine, host, now)
            updated.append(
                {
                    "machine_id": str(machine.id),
                    "name": host["bmh_name"],
                    "pci_addresses": host.get("pci_addresses", []),
                    "pci_inventory_adopted": adopted_pci,
                }
            )
    record.update(
        {
            "host_count": len(hosts),
            "matched": len(updated),
            "unmatched": unmatched[:64],
            "warnings": _bounded_warnings(output.get("warnings")),
        }
    )
    return record, updated, None


def reconcile_deployment(deployment, *, client=None) -> dict[str, Any]:
    """Read both action results and store whatever the EPH reported since.

    Missing runs and bridge outages are normal states rather than errors, so a
    deployment that has not reached the inspection step reconciles to "unchanged"
    instead of failing the read that triggered it.
    """

    owned_client = None
    try:
        if client is None:
            owned_client = build_cloud_control_client()
            client = owned_client
        changed: dict[str, bool] = {}
        for key, action in (
            (EPH_PREFLIGHT_KEY, EPH_PREFLIGHT_ACTION),
            (BMH_INVENTORY_KEY, BMH_INSPECT_ACTION),
        ):
            try:
                payload = client.get_deployment_action_result(deployment.id, action)
            except CloudControlDispatchError:
                continue
            if not isinstance(payload, dict):
                continue
            if not payload.get("available"):
                continue
            if key == EPH_PREFLIGHT_KEY:
                changed[key] = store_snapshot(
                    deployment,
                    key,
                    eph_preflight_snapshot(payload),
                )
            else:
                record, _updated, error = bmh_inventory_snapshot(deployment, payload)
                # A failed inspection is a verdict too: storing only successful
                # runs left the page showing the previous green snapshot.
                if error is None or record.get("status") is not None:
                    changed[key] = store_snapshot(deployment, key, record)
        return changed
    finally:
        if owned_client is not None:
            owned_client.close()


def reconcile_deployment_quietly(deployment) -> None:
    """Best-effort reconcile for read paths; must never fail the read."""

    try:
        reconcile_deployment(deployment)
    except (CloudControlNotConfigured, CloudControlBridgeError, OSError):
        # No bridge configured or unreachable: the detail page still renders.
        return
    except Exception:  # pragma: no cover - defensive, logged not raised
        logger.warning(
            "deployment reconcile failed for %s",
            deployment.id,
            exc_info=True,
        )


__all__ = [
    "BMH_INSPECT_ACTION",
    "BMH_INVENTORY_KEY",
    "EPH_PREFLIGHT_ACTION",
    "EPH_PREFLIGHT_KEY",
    "bmh_inventory_snapshot",
    "eph_preflight_snapshot",
    "reconcile_deployment",
    "reconcile_deployment_quietly",
    "store_snapshot",
]
