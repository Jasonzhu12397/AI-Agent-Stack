"""Alipay adapter with real outbound HTTP and RSA2 (SHA256withRSA) signing.

Request parameters are sorted by key, joined as ``key=value`` pairs, signed
with the merchant private key under RSA2, and posted as an
``application/x-www-form-urlencoded`` form to the Alipay gateway. Async notify
bodies are verified by stripping ``sign`` / ``sign_type`` and applying the
same sorted-content rule against the Alipay public key.

Credentials are ``env://`` / ``vault://`` references resolved at call time;
unresolved references keep the provider out of the registry. The private key
and public key material never appear in returned receipts or audit metadata.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core.payments.base import (
    CallbackResult,
    PaymentProvider,
    PaymentProviderError,
    ProviderOrderState,
    resolve_credential_ref,
)
from core.payments.http import build_client, send_with_retry

_TRADE_STATUS_MAP = {
    "TRADE_SUCCESS": "paid",
    "TRADE_FINISHED": "paid",
    "TRADE_CLOSED": "closed",
    "WAIT_BUYER_PAY": "pending",
}

_DEFAULT_GATEWAY = "https://openapi.alipay.com/gateway.do"


def _load_key(pem: str, private: bool):
    if "BEGIN" not in pem:
        header = "PRIVATE KEY" if private else "PUBLIC KEY"
        pem = f"-----BEGIN {header}-----\n{pem}\n-----END {header}-----\n"
    if private:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    return serialization.load_pem_public_key(pem.encode("utf-8"))


def _yuan_to_cents(amount: str) -> int | None:
    try:
        return int((Decimal(amount.strip()) * 100).to_integral_value())
    except (InvalidOperation, AttributeError, ValueError):
        return None


class AlipayProvider(PaymentProvider):
    channel = "alipay"

    def __init__(
        self,
        app_id_ref: str = "env://ALIPAY_APP_ID",
        private_key_ref: str = "env://ALIPAY_PRIVATE_KEY",
        public_key_ref: str = "env://ALIPAY_PUBLIC_KEY",
        notify_url_ref: str = "env://ALIPAY_NOTIFY_URL",
        gateway_ref: str = "env://ALIPAY_GATEWAY",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._refs = {
            "app_id": app_id_ref,
            "private_key": private_key_ref,
            "public_key": public_key_ref,
            "notify_url": notify_url_ref,
            "gateway": gateway_ref,
        }
        self._transport = transport
        self._client: httpx.Client | None = None

    def _resolve(self, key: str) -> str | None:
        return resolve_credential_ref(self._refs[key])

    def is_configured(self) -> bool:
        required = ("app_id", "private_key", "public_key")
        return all(self._resolve(key) is not None for key in required)

    # ------------------------------------------------------------- internals

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = build_client(transport=self._transport)
        return self._client

    def _gateway(self) -> str:
        return (self._resolve("gateway") or _DEFAULT_GATEWAY).rstrip("/")

    @staticmethod
    def _signed_content(params: dict[str, str]) -> str:
        excluded = {"sign", "sign_type"}
        items = sorted(
            (key, value)
            for key, value in params.items()
            if key not in excluded and value != ""
        )
        return "&".join(f"{key}={value}" for key, value in items)

    def _sign(self, params: dict[str, str]) -> str:
        private_key = _load_key(self._resolve("private_key") or "", private=True)
        content = self._signed_content(params).encode("utf-8")
        signature = private_key.sign(content, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("utf-8")

    def _verify(self, params: dict[str, str], signature: str) -> bool:
        try:
            public_key = _load_key(self._resolve("public_key") or "", private=False)
            content = self._signed_content(params).encode("utf-8")
            public_key.verify(
                base64.b64decode(signature),
                content,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception:  # noqa: BLE001 - any verify failure is invalid
            return False

    def _common_params(self, method: str) -> dict[str, str]:
        return {
            "app_id": self._resolve("app_id") or "",
            "method": method,
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "notify_url": self._resolve("notify_url") or "",
        }

    def _request(self, method: str, biz_content: dict) -> dict:
        params = self._common_params(method)
        params["biz_content"] = json.dumps(biz_content, ensure_ascii=False)
        params["sign"] = self._sign(params)

        response = send_with_retry(
            self._client_instance(),
            "POST",
            self._gateway(),
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR",
                f"Alipay returned HTTP {response.status_code}.",
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR",
                "Alipay returned an unparsable response.",
            ) from exc

        node_key = method.replace(".", "_") + "_response"
        node = envelope.get(node_key) or {}
        if node.get("code") not in {"10000"}:
            decline_reason = node.get("sub_code") or node.get("msg") or "unknown"
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR",
                f"Alipay declined the request: {decline_reason}.",
            )
        return node

    # ------------------------------------------------------------------ orders

    def create_payment(self, order) -> dict:
        biz_content = {
            "out_trade_no": str(order.id),
            "total_amount": f"{order.amount_cents / 100:.2f}",
            "subject": f"Order {order.id} - {order.plan.code}",
            "product_code": "QRCODE_OFFLINE",
        }
        node = self._request("alipay.trade.precreate", biz_content)
        return {
            "provider": "alipay",
            "trade_no": str(order.id),
            "qr_code": node.get("qr_code", ""),
        }

    def query_order(self, order) -> ProviderOrderState:
        biz_content = {"out_trade_no": str(order.id)}
        node = self._request("alipay.trade.query", biz_content)
        return ProviderOrderState(
            status=_TRADE_STATUS_MAP.get(node.get("trade_status", ""), "unknown"),
            amount_cents=_yuan_to_cents(node.get("total_amount", "") or ""),
        )

    def refund(
        self, order, reason: str, amount_cents: int | None = None
    ) -> dict:
        refund_cents = amount_cents if amount_cents is not None else order.amount_cents
        refund_amount = (
            Decimal(refund_cents) / Decimal(100)
        ).quantize(Decimal("0.01"))
        biz_content = {
            "out_trade_no": str(order.id),
            "refund_amount": f"{refund_amount:.2f}",
            "refund_reason": reason[:200],
        }
        node = self._request("alipay.trade.refund", biz_content)
        return {
            "provider": "alipay",
            "refund_id": node.get("trade_no", ""),
            "status": node.get("trade_status", ""),
            "refund_amount": node.get("refund_fee", ""),
        }

    # ------------------------------------------------------------- callbacks

    def verify_callback(self, headers, body: bytes) -> CallbackResult:
        try:
            params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError as exc:
            return CallbackResult(ok=False, message=f"body decode failed: {exc}")

        signature = params.get("sign", "")
        if not signature:
            return CallbackResult(ok=False, message="missing sign parameter")
        if not self._verify(params, signature):
            return CallbackResult(ok=False, message="RSA2 signature invalid")

        amount_cents = _yuan_to_cents(params.get("total_amount", ""))
        return CallbackResult(
            ok=True,
            status=_TRADE_STATUS_MAP.get(params.get("trade_status", ""), "unknown"),
            order_reference=params.get("out_trade_no"),
            raw=params,
            amount_cents=amount_cents,
        )
