"""WeChat Pay APIv3 adapter skeleton.

Implements the real v3 protocol shapes: the JSON下单 order payload, the
``Wechatpay-Serial`` / ``Wechatpay-Signature`` / ``Wechatpay-Timestamp`` /
``Wechatpay-Nonce`` header verification, AES-256-GCM resource decryption,
order query and refund entry points. Network transport is intentionally left
to a thin helper so the protocol surface stays testable offline.

All credentials are ``env://`` / ``vault://`` references resolved at call
time; missing references keep the provider out of the registry.
"""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.payments.base import CallbackResult, PaymentProvider, resolve_credential_ref

# Trade state (v3 `trade_state`) mapped to the internal order status.
_TRADE_STATE_MAP = {
    "SUCCESS": "paid",
    "PAYERROR": "failed",
    "CLOSED": "closed",
    "REVOKED": "refunded",
}


class WechatPayV3Provider(PaymentProvider):
    channel = "wechat"

    def __init__(
        self,
        appid_ref: str = "env://WECHATPAY_APPID",
        mchid_ref: str = "env://WECHATPAY_MCHID",
        api_v3_key_ref: str = "env://WECHATPAY_API_V3_KEY",
        serial_no_ref: str = "env://WECHATPAY_CERT_SERIAL_NO",
        private_key_ref: str = "env://WECHATPAY_PRIVATE_KEY",
        platform_cert_ref: str = "env://WECHATPAY_PLATFORM_CERT",
        notify_url_ref: str = "env://WECHATPAY_NOTIFY_URL",
    ) -> None:
        self._refs = {
            "appid": appid_ref,
            "mchid": mchid_ref,
            "api_v3_key": api_v3_key_ref,
            "serial_no": serial_no_ref,
            "private_key": private_key_ref,
            "platform_cert": platform_cert_ref,
            "notify_url": notify_url_ref,
        }

    def _resolve(self, key: str) -> str | None:
        return resolve_credential_ref(self._refs[key])

    def is_configured(self) -> bool:
        required = ("appid", "mchid", "api_v3_key", "serial_no", "private_key")
        return all(self._resolve(key) is not None for key in required)

    # ------------------------------------------------------------------ orders

    def create_payment(self, order) -> dict:
        appid = self._resolve("appid")
        mchid = self._resolve("mchid")
        notify_url = self._resolve("notify_url") or ""
        payload = {
            "appid": appid,
            "mchid": mchid,
            "description": f"Order {order.id} - {order.plan.code}",
            "out_trade_no": str(order.id),
            "notify_url": notify_url,
            "amount": {"total": order.amount_cents, "currency": order.currency},
        }
        return {
            "provider": "wechat_v3",
            "trade_no": str(order.id),
            "order_payload": payload,
        }

    def query_order(self, order) -> str:
        trade_no = order.provider_trade_no or str(order.id)
        # GET /v3/pay/transactions/out-trade-no/{out_trade_no}?mchid=...
        return f"wechat://out-trade-no/{trade_no}"

    def refund(self, order, reason: str) -> dict:
        payload = {
            "out_trade_no": str(order.id),
            "out_refund_no": f"refund-{order.id}",
            "reason": reason[:200],
            "amount": {
                "refund": order.amount_cents,
                "total": order.amount_cents,
                "currency": order.currency,
            },
        }
        return {"provider": "wechat_v3", "refund_payload": payload}

    # ------------------------------------------------------------- callbacks

    def verify_callback(self, headers, body: bytes) -> CallbackResult:
        required = (
            "Wechatpay-Timestamp",
            "Wechatpay-Nonce",
            "Wechatpay-Serial",
            "Wechatpay-Signature",
        )
        missing = [name for name in required if not headers.get(name)]
        if missing:
            return CallbackResult(ok=False, message=f"missing headers: {', '.join(missing)}")

        platform_cert_pem = self._resolve("platform_cert")
        api_v3_key = self._resolve("api_v3_key")
        if not platform_cert_pem or not api_v3_key:
            # Fail closed: without the platform certificate we cannot prove
            # the callback came from WeChat Pay.
            return CallbackResult(ok=False, message="platform certificate not configured")

        timestamp = headers["Wechatpay-Timestamp"]
        nonce = headers["Wechatpay-Nonce"]
        signature = base64.b64decode(headers["Wechatpay-Signature"])
        message = f"{timestamp}\n{nonce}\n".encode() + body + b"\n"

        try:
            public_key = serialization.load_pem_public_key(
                platform_cert_pem.encode("utf-8")
            )
            public_key.verify(
                signature,
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:  # noqa: BLE001 - any verify failure is invalid
            return CallbackResult(ok=False, message=f"signature invalid: {exc}")

        try:
            envelope = json.loads(body.decode("utf-8"))
            resource = envelope["resource"]
            plaintext = self._decrypt_resource(resource, api_v3_key)
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return CallbackResult(ok=False, message=f"resource decode failed: {exc}")

        trade_state = plaintext.get("trade_state", "unknown")
        return CallbackResult(
            ok=True,
            status=_TRADE_STATE_MAP.get(trade_state, "unknown"),
            order_reference=plaintext.get("out_trade_no"),
            raw=plaintext,
        )

    @staticmethod
    def _decrypt_resource(resource: dict, api_v3_key: str) -> dict:
        ciphertext = base64.b64decode(resource["ciphertext"])
        nonce = resource["nonce"]
        associated_data = resource.get("associated_data", "")
        aesgcm = AESGCM(api_v3_key.encode("utf-8"))
        plaintext = aesgcm.decrypt(
            nonce.encode("utf-8"),
            ciphertext,
            associated_data.encode("utf-8") if associated_data else None,
        )
        return json.loads(plaintext.decode("utf-8"))
