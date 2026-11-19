"""Classify Agent command outcomes for safe controller-side retry."""

from __future__ import annotations

from typing import Any

_TRANSIENT_MARKERS = (
    "_API_UNAVAILABLE",
    "_READY_TIMEOUT",
    "_WAIT_TIMEOUT",
    "_REQUEST_TIMEOUT",
    "_PROBE_TIMEOUT",
)


def is_retryable_outcome(outcome: dict[str, Any]) -> bool:
    """Return whether a failed command may be requeued without operator input.

    Blocked outcomes encode policy or missing prerequisite facts and are never
    retried. A failed action that already mutated resources is retryable only
    because every Agent mutation adapter is idempotent; transient API and
    timeout failures are retryable even when they failed before mutating.
    """

    if outcome.get("status") != "failed":
        return False
    if outcome.get("side_effects") is True:
        return True
    code = str(outcome.get("error_code") or "")
    return any(marker in code for marker in _TRANSIENT_MARKERS)
