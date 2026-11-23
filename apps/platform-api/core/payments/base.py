"""Payment provider abstraction and shared credential-reference resolution.

Credentials are never stored inline. Every secret is referenced as an
``env://NAME`` or ``vault://path`` string and resolved at call time. A provider
whose required references do not resolve reports ``is_configured() == False``
and stays out of the registry.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


def resolve_credential_ref(ref: str | None) -> str | None:
    """Resolve a secret reference to its plaintext value, or ``None``.

    ``env://NAME`` reads the process environment. ``vault://path`` references
    the platform secret resolver; an unresolved vault reference means the
    provider is not configured in this deployment.
    """

    if not ref:
        return None
    if ref.startswith("env://"):
        name = ref[len("env://") :]
        value = os.environ.get(name, "").strip()
        return value or None
    if ref.startswith("vault://"):
        return None
    return None


@dataclass
class CallbackResult:
    """Outcome of verifying a provider callback payload."""

    ok: bool
    status: str = "unknown"  # paid | failed | closed | unknown
    order_reference: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    """Interface every payment channel adapter must implement."""

    channel: str

    @abstractmethod
    def is_configured(self) -> bool:
        """True when every required credential reference resolves."""

    @abstractmethod
    def create_payment(self, order) -> dict[str, Any]:
        """Build the provider-side payment order and return instructions.

        The returned dict must carry ``trade_no`` (the provider trade number
        persisted on the order) plus any payment instructions the client
        needs to complete checkout.
        """

    @abstractmethod
    def verify_callback(self, headers, body: bytes) -> CallbackResult:
        """Verify signature and decrypt/parse a callback payload.

        Fail-closed: any missing or invalid signature, unparseable body or
        decryption failure yields ``ok=False``.
        """

    @abstractmethod
    def query_order(self, order) -> str:
        """Return the provider-side trade state for an existing order."""

    @abstractmethod
    def refund(self, order, reason: str) -> dict[str, Any]:
        """Issue a refund for a paid order; returns the provider receipt."""
