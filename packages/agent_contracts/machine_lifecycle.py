"""Lifecycle states for a bare-metal machine and the transitions between them.

The lifecycle is a contract, not a view detail: the platform enforces the same
transitions the UI offers, and the API hands the UI the allowed targets for the
current state so no client has to duplicate the table.
"""

from __future__ import annotations

DISCOVERED = "discovered"
IDENTITY_REVIEW = "identity_review"
COMMISSIONED = "commissioned"
RETIRED = "retired"

STATES = (DISCOVERED, IDENTITY_REVIEW, COMMISSIONED, RETIRED)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    DISCOVERED: (IDENTITY_REVIEW,),
    IDENTITY_REVIEW: (COMMISSIONED, DISCOVERED),
    COMMISSIONED: (RETIRED,),
    RETIRED: (DISCOVERED,),
}

# Actions that put a machine back on the wire. A retired machine must be returned
# to ``discovered`` (an explicit lifecycle review) before it may be booted again,
# and an unknown state is treated the same way: fail closed.
BOOT_ACTIONS = ("pxe_boot", "discovery")
BOOTABLE_STATES = (DISCOVERED, IDENTITY_REVIEW, COMMISSIONED)

REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500


def allowed_targets(state: str | None) -> tuple[str, ...]:
    """Return the states a machine in ``state`` may move to.

    An unknown state has no transitions: the caller must re-read the machine
    rather than guess which repair is legitimate.
    """

    if not isinstance(state, str):
        return ()
    return TRANSITIONS.get(state, ())


def transition_allowed(expected_state: str | None, target_state: str | None) -> bool:
    if not isinstance(expected_state, str) or not isinstance(target_state, str):
        return False
    return target_state in allowed_targets(expected_state)


def action_blocked(state: str | None, action: str) -> bool:
    """Return True when the machine state forbids the requested boot action."""

    if action not in BOOT_ACTIONS:
        raise ValueError(f"unknown machine action: {action}")
    return not isinstance(state, str) or state not in BOOTABLE_STATES


def normalize_reason(reason: object) -> str:
    """Return the trimmed review reason, or an empty string when it is unusable."""

    if not isinstance(reason, str):
        return ""
    trimmed = reason.strip()
    if not REASON_MIN_LENGTH <= len(trimmed) <= REASON_MAX_LENGTH:
        return ""
    return trimmed
