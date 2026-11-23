"""Payment provider abstraction and shared credential-reference resolution.

Credentials are never stored inline. Every secret is referenced as an
``env://NAME`` or ``vault://mount/path#field`` string and resolved at call time
through the platform credential resolver, so payment channels follow exactly the
same reference syntax and allowlist rules as plugin connectors. A provider
whose required references do not resolve reports ``is_configured() == False``
and stays out of the registry.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from core.plugin_credentials import CredentialError, resolve_plaintext

DEFAULT_PAYMENT_ENV_PREFIXES = ("WECHATPAY_", "ALIPAY_")


def payment_env_prefixes() -> tuple[str, ...]:
    """Environment variable prefixes a payment channel may read."""

    configured = getattr(
        settings, "PAYMENT_CREDENTIAL_ENV_PREFIXES", DEFAULT_PAYMENT_ENV_PREFIXES
    )
    prefixes = tuple(str(prefix) for prefix in configured if str(prefix))
    return prefixes or DEFAULT_PAYMENT_ENV_PREFIXES


def resolve_credential_ref(ref: str | None) -> str | None:
    """Resolve a secret reference to its plaintext value, or ``None``.

    ``None`` means the reference did not resolve in this deployment, which keeps
    the channel out of the registry instead of failing a request. Resolution is
    delegated to ``core.plugin_credentials.resolve_plaintext``: environment
    lookups are confined to the payment prefixes, and ``vault://`` references
    are read from the deployment's secret store rather than silently ignored.
    """

    if not ref:
        return None
    try:
        value = resolve_plaintext(ref, env_prefixes=payment_env_prefixes())
    except CredentialError:
        return None
    return value or None


@dataclass
class CallbackResult:
    """Outcome of verifying a provider callback payload."""

    ok: bool
    status: str = "unknown"  # paid | failed | closed | refunded | unknown
    order_reference: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    amount_cents: int | None = None
    refund_reference: str | None = None


@dataclass
class ProviderOrderState:
    """Provider-side trade state returned by ``query_order``.

    ``amount_cents`` is ``None`` when the provider query does not expose an
    amount; the reconciliation comparison then skips the amount check.
    """

    status: str
    amount_cents: int | None = None


class PaymentProviderError(Exception):
    """Upstream provider transport or protocol failure.

    Carries a stable error code for the response envelope; the message is
    safe to surface. Secrets never travel in this exception.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class PaymentProvider(ABC):
    """Interface every payment channel adapter must implement."""

    channel: str

    # Credential slots this adapter reads. ``SLOTS`` lists every slot in the
    # order it should be reported; ``REQUIRED_SLOTS`` is the subset that must
    # resolve for the channel to be usable. Readiness, the missing-slot report
    # and the registry filter all derive from these two declarations, so an
    # adapter never restates its own configuration rule.
    SLOTS: tuple[str, ...] = ()
    REQUIRED_SLOTS: tuple[str, ...] = ()

    def references(self) -> dict[str, str]:
        """Credential reference URI per slot, in declaration order."""

        return {slot: self._refs[slot] for slot in self.SLOTS if slot in self._refs}

    def resolve_slot(self, slot: str) -> str | None:
        """Resolve one declared slot to its plaintext, or ``None``."""

        return resolve_credential_ref(self.references().get(slot))

    def missing_slots(self) -> list[str]:
        """Required slots that do not resolve in this deployment."""

        return [slot for slot in self.REQUIRED_SLOTS if self.resolve_slot(slot) is None]

    def is_configured(self) -> bool:
        """True when every required credential reference resolves."""

        return not self.missing_slots()

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
    def query_order(self, order) -> ProviderOrderState:
        """Return the provider-side trade state for an existing order."""

    @abstractmethod
    def refund(
        self, order, reason: str, amount_cents: int | None = None
    ) -> dict[str, Any]:
        """Issue a refund for a paid order; returns the provider receipt.

        ``amount_cents`` is the slice being refunded; ``None`` means the full
        order amount.
        """
