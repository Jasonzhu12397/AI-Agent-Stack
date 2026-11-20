"""Fail-closed network evidence gate for the Target handoff.

The platform binds every deployment to the exact ``AgentMachine`` inventory
revision its network readiness was validated against. The Target Agent hands a
cluster over from EPH control to the target control plane months later in human
terms, and nodes may have been re-collected in between. Re-imaging or re-cabling
a node changes its observed revision, so the physical facts the readiness gate
approved are no longer the physical facts on the floor.

This module re-checks those bindings inside the control plane, at the moment the
Target handoff command is enqueued. It runs before any command row is inserted,
so a drifted fleet cannot start worker provisioning at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

EVIDENCE_MISSING = "NETWORK_EVIDENCE_MISSING"
EVIDENCE_DRIFT = "NETWORK_EVIDENCE_DRIFT"
EVIDENCE_UNAVAILABLE = "NETWORK_EVIDENCE_UNAVAILABLE"


class NetworkEvidenceError(ValueError):
    """Raised when the bound network evidence no longer matches the fleet."""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _same_timestamp(left: Any, right: Any) -> bool:
    left_value = _parse_datetime(left)
    right_value = _parse_datetime(right)
    if left_value is None or right_value is None:
        return left == right
    return left_value == right_value


def evidence_entries(spec: Any) -> list[dict[str, Any]] | None:
    """Return the bound evidence list, or ``None`` when nothing was bound."""

    if not isinstance(spec, dict):
        return None
    evidence = spec.get("network_evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    return evidence


def network_evidence_required(spec: Any) -> bool:
    """True when this deployment claims a network profile and needs evidence."""

    if not isinstance(spec, dict):
        return False
    return bool(evidence_entries(spec)) or spec.get("infra_network_profile") is not None


def drift_report(
    entries: list[dict[str, Any]],
    machines: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare bound evidence with the current machine revisions."""

    drift: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            drift.append({"machine_id": None, "code": "NETWORK_EVIDENCE_INVALID"})
            continue
        raw_id = entry.get("machine_id")
        try:
            machine_id = str(UUID(str(raw_id)))
        except (TypeError, ValueError):
            drift.append({"machine_id": raw_id, "code": "NETWORK_EVIDENCE_INVALID"})
            continue
        machine = machines.get(machine_id)
        if machine is None:
            drift.append(
                {
                    "machine_id": machine_id,
                    "code": "NETWORK_EVIDENCE_MACHINE_MISSING",
                }
            )
            continue
        revision = entry.get("inventory_revision")
        current_revision = machine.get("inventory_revision")
        observed_at = entry.get("observed_at")
        current_observed_at = machine.get("observed_at")
        if revision != current_revision or not _same_timestamp(
            observed_at, current_observed_at
        ):
            drift.append(
                {
                    "machine_id": machine_id,
                    "code": "NETWORK_EVIDENCE_STALE",
                    "expected_revision": revision,
                    "current_revision": current_revision,
                    "expected_observed_at": observed_at,
                    "current_observed_at": current_observed_at,
                }
            )
    return drift


def require_current_network_evidence(
    spec: Any,
    machines: dict[str, dict[str, Any]],
) -> None:
    """Raise when the deployment's bound evidence is missing or stale."""

    if not network_evidence_required(spec):
        return
    entries = evidence_entries(spec)
    if entries is None:
        raise NetworkEvidenceError(EVIDENCE_MISSING)
    drift = drift_report(entries, machines)
    if drift:
        raise NetworkEvidenceError(EVIDENCE_DRIFT)
