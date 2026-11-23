"""Alipay adapter skeleton (RSA2 / SHA256withRSA).

Covers ``alipay.trade.create``, ``alipay.trade.query`` and
``alipay.trade.refund`` request shapes plus async-notify signature
verification. Credentials are ``env://`` / ``vault://`` references resolved at
call time; unresolved references keep the provider out of the registry.
"""

import base64
import json
from urllib.parse import parse_qsl

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core.payments.base import CallbackResult, PaymentProvider, resolve_credential_ref

_TRADE_STATUS_MAP = {
    "TRADE_SUCCESS": "paid",
    "TRADE_FINISHED": "paid",
    "TRADE_CLOSED": "closed",
    "WAIT_BUYER_PAY": "pending",
}


def _load_key(pem: str, private: bool):
    if "BEGIN" not in pem:
        header = "PRIVATE KEY" if private else "PUBLIC KEY"
        pem = f"-----BEGIN {header}-----\n{pem}\n-----END {header}-----\n"
    if private:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    return serialization.load_pem_public_key(pem.encode("utf-8"))


class AlipayProvider(PaymentProvider):
    channel = "alipay"

    def __init__(
        self,
        app_id_ref: str = "env://ALIPAY_APP_ID",
        private_key_ref: str = "env://ALIPAY_PRIVATE_KEY",
        public_key_ref: str = "env://ALIPAY_PUBLIC_KEY",
        notify_url_ref: str = "env://ALIPAY_NOTIFY_URL",
        gateway_ref: str = "env://ALIPAY_GATEWAY",
    ) -> None:
        self._refs = {
            "app_id": app_id_ref,
            "private_key": private_key_ref,
            "public_key": public_key_ref,
            "notify_url": notify_url_ref,
            "gateway": gateway_ref,
        }

    def _resolve(self, key: str) -> str | None:
        return resolve_credential_ref(self._refs[key])

    def is_configured(self) -> bool:
        required = ("app_id", "private_key", "public_key")
        return all(self._resolve(key) is not None for key in required)

    # ------------------------------------------------------------------ signing

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
        private_key = _load_key(self._resolve("private_key"), private=True)
        content = self._signed_content(params).encode("utf-8")
        signature = private_key.sign(content, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("utf-8")

    def _verify(self, params: dict[str, str], signature: str) -> bool:
        try:
            public_key = _load_key(self._resolve("public_key"), private=False)
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

    # ------------------------------------------------------------------ orders

    def _common_params(self, method: str) -> dict[str, str]:
        return {
            "app_id": self._resolve("app_id") or "",
            "method": method,
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": "",
            "version": "1.0",
            "notify_url": self._resolve("notify_url") or "",
        }

    def create_payment(self, order) -> dict:
        biz_content = {
            "out_trade_no": str(order.id),
            "total_amount": f"{order.amount_cents / 100:.2f}",
            "subject": f"Order {order.id} - {order.plan.code}",
            "product_code": "JSAPI_PAY",
        }
        params = self._common_params("alipay.trade.create")
        params["biz_content"] = json.dumps(biz_content, ensure_ascii=False)
        params["sign"] = self._sign(params)
        return {
            "provider": "alipay",
            "trade_no": str(order.id),
            "request": params,
        }

    def query_order(self, order) -> str:
        return f"alipay://out-trade-no/{order.provider_trade_no or order.id}"

    def refund(self, order, reason: str) -> dict:
        biz_content = {
            "out_trade_no": str(order.id),
            "refund_amount": f"{order.amount_cents / 100:.2f}",
            "refund_reason": reason[:200],
        }
        return {"provider": "alipay", "biz_content": biz_content}

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

        trade_status = params.get("trade_status", "unknown")
        return CallbackResult(
            ok=True,
            status=_TRADE_STATUS_MAP.get(trade_status, "unknown"),
            order_reference=params.get("out_trade_no"),
            raw=params,
        )
