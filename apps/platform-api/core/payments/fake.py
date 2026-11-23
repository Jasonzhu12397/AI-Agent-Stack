"""Test-double payment provider.

Never enabled by default. The registry only registers this provider when the
explicit settings override ``BILLING_FAKE_PROVIDER_ENABLED`` is true, which
tests set directly. Callbacks carry a shared test signature header so the
fail-closed verification path is still exercised.
"""

import json

from core.payments.base import CallbackResult, PaymentProvider, ProviderOrderState

FAKE_SIGNATURE_HEADER = "X-Fake-Signature"
FAKE_SIGNATURE_VALUE = "fake-ok"


class FakeProvider(PaymentProvider):
    channel = "wechat"

    def is_configured(self) -> bool:
        return True

    def create_payment(self, order) -> dict:
        trade_no = f"fake-{order.id}"
        return {
            "provider": "fake",
            "trade_no": trade_no,
            "prepay_id": f"fake-prepay-{order.id}",
        }

    def verify_callback(self, headers, body: bytes) -> CallbackResult:
        if headers.get(FAKE_SIGNATURE_HEADER) != FAKE_SIGNATURE_VALUE:
            return CallbackResult(ok=False, message="fake signature missing or invalid")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return CallbackResult(ok=False, message=f"body decode failed: {exc}")
        status = payload.get("status", "unknown")
        if status not in {"paid", "failed", "closed"}:
            return CallbackResult(ok=False, message=f"unknown status: {status}")
        return CallbackResult(
            ok=True,
            status=status,
            order_reference=payload.get("out_trade_no"),
            raw=payload,
        )

    def query_order(self, order) -> ProviderOrderState:
        return ProviderOrderState(status="unknown", amount_cents=None)

    def refund(
        self, order, reason: str, amount_cents: int | None = None
    ) -> dict:
        return {
            "provider": "fake",
            "refunded": True,
            "reason": reason,
            "refund_id": f"refund-{order.id}-{amount_cents}",
        }
