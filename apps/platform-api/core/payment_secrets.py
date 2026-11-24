"""Payment merchant credentials entered by an operator, sealed at rest.

Merchant keys normally arrive as environment variables on the deployment, which
means a self-hosted instance cannot finish a payment channel from the console.
This store closes that gap: an operator pastes the merchant credentials once,
they are sealed with AES-GCM under a key derived from ``SECRET_KEY``, and the
payment adapters resolve a slot from here when the environment reference is
absent. The plaintext never touches a response body or an audit record.
"""

from __future__ import annotations

from core.field_crypto import open_sealed as _open
from core.field_crypto import seal as _seal

_SCOPE = "payment-channel-secret"


def seal(plaintext: str) -> str:
    """Encrypt one merchant credential into a storable string."""

    return _seal(plaintext, _SCOPE)


def open_sealed(blob: str) -> str | None:
    """Decrypt a stored credential, or ``None`` when it cannot be read."""

    return _open(blob, _SCOPE)


def stored_secret(channel: str, slot: str) -> str | None:
    """Plaintext for a channel slot saved from the console, if any."""

    from core.billing_models import PaymentChannelSecret

    try:
        record = PaymentChannelSecret.objects.filter(channel=channel, slot=slot).first()
    except Exception:
        # Adapter unit tests run without a database; a slot that cannot be read
        # is simply unresolved, which keeps the channel out of the registry.
        return None
    if record is None:
        return None
    return open_sealed(record.sealed_value)


def save_channel_secrets(channel: str, values: dict[str, str], user=None) -> None:
    """Seal and store the given slots, leaving unspecified slots untouched."""

    from core.billing_models import PaymentChannelSecret

    for slot, value in values.items():
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            PaymentChannelSecret.objects.filter(channel=channel, slot=slot).delete()
            continue
        PaymentChannelSecret.objects.update_or_create(
            channel=channel,
            slot=slot,
            defaults={"sealed_value": seal(value), "updated_by": user},
        )


def stored_slots(channel: str) -> dict[str, bool]:
    """Which slots the console has filled, without revealing any value."""

    from core.billing_models import PaymentChannelSecret

    return {record.slot: True for record in PaymentChannelSecret.objects.filter(channel=channel)}
