"""WeChat Pay APIv3 adapter with real outbound HTTP and signature path.

Request signing follows the v3 spec: the signature string is
``METHOD\\nTARGET\\nTIMESTAMP\\nNONCE\\nBODY\\n`` signed with the merchant
private key under RSA-SHA256 (PKCS#1 v1.5), and the result travels in a
``WECHATPAY2-SHA256-RSA2048`` Authorization header.

Callback verification is fail-closed: the ``Wechatpay-Serial`` header must
match the configured platform certificate serial number, the platform cert
public key verifies ``timestamp\\nnonce\\nbody\\n``, and the ``resource``
field is decrypted with AES-256-GCM using the api_v3 key.

All credentials are ``env://`` / ``vault://`` references resolved at call time.
Neither the private key, the api_v3 key nor any PEM material is copied into
returned receipts, logs or audit metadata.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.payments.base import (
    CallbackResult,
    PaymentProvider,
    PaymentProviderError,
    ProviderOrderState,
)
from core.payments.http import build_client, send_with_retry

# Trade state (v3 `trade_state`) mapped to the internal order status.
_TRADE_STATE_MAP = {
    "SUCCESS": "paid",
    "PAYERROR": "failed",
    "CLOSED": "closed",
    "REVOKED": "refunded",
}

# Refund notify `refund_status` mapped to internal semantics. ABNORMAL and
# CLOSED are recorded but never move the order state or reclaim quota.
_REFUND_STATUS_MAP = {
    "SUCCESS": "refunded",
    "PROCESSING": "pending",
    "ABNORMAL": "refund_abnormal",
    "CLOSED": "refund_abnormal",
}

_DEFAULT_BASE_URL = "https://api.mch.weixin.qq.com"

# Response signatures are accepted only within this clock-skew window, so a
# captured response body cannot be replayed indefinitely.
_RESPONSE_SKEW_SECONDS = 300


class WechatPayV3Provider(PaymentProvider):
    channel = "wechat"

    SLOTS = (
        "appid",
        "mchid",
        "api_v3_key",
        "serial_no",
        "private_key",
        "platform_cert",
        "notify_url",
        "base_url",
    )
    # ``notify_url`` is required too: WeChat rejects a native order without a
    # reachable callback URL, and a trade we cannot receive notifications for
    # would be stranded after the buyer pays.
    REQUIRED_SLOTS = (
        "appid",
        "mchid",
        "api_v3_key",
        "serial_no",
        "private_key",
        "platform_cert",
        "notify_url",
    )

    def __init__(
        self,
        appid_ref: str = "env://WECHATPAY_APPID",
        mchid_ref: str = "env://WECHATPAY_MCHID",
        api_v3_key_ref: str = "env://WECHATPAY_API_V3_KEY",
        serial_no_ref: str = "env://WECHATPAY_CERT_SERIAL_NO",
        private_key_ref: str = "env://WECHATPAY_PRIVATE_KEY",
        platform_cert_ref: str = "env://WECHATPAY_PLATFORM_CERT",
        notify_url_ref: str = "env://WECHATPAY_NOTIFY_URL",
        base_url_ref: str = "env://WECHATPAY_BASE_URL",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._refs = {
            "appid": appid_ref,
            "mchid": mchid_ref,
            "api_v3_key": api_v3_key_ref,
            "serial_no": serial_no_ref,
            "private_key": private_key_ref,
            "platform_cert": platform_cert_ref,
            "notify_url": notify_url_ref,
            "base_url": base_url_ref,
        }
        self._transport = transport
        self._client: httpx.Client | None = None

    def _resolve(self, key: str) -> str | None:
        return self.resolve_slot(key)

    # ------------------------------------------------------------- internals

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = build_client(transport=self._transport)
        return self._client

    def _base_url(self) -> str:
        return (self._resolve("base_url") or _DEFAULT_BASE_URL).rstrip("/")

    def _load_private_key(self):
        pem = self._resolve("private_key") or ""
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)

    def _platform_certificate(self):
        pem = self._resolve("platform_cert") or ""
        return x509.load_pem_x509_certificate(pem.encode("utf-8"))

    def _verify_response(self, response: httpx.Response) -> None:
        """Verify the platform signature on a successful response body.

        WeChat signs ``timestamp\nnonce\nbody\n`` with the platform key and
        identifies the signing certificate by serial. Without this check a
        compromised or spoofed upstream could hand back attacker-chosen
        trade states. An empty body (204 close) carries nothing to verify.
        """

        if not response.content:
            return
        timestamp = response.headers.get("Wechatpay-Timestamp", "")
        nonce = response.headers.get("Wechatpay-Nonce", "")
        signature = response.headers.get("Wechatpay-Signature", "")
        serial = response.headers.get("Wechatpay-Serial", "")
        if not (timestamp and nonce and signature and serial):
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED",
                "WeChat Pay response is missing its signature headers.",
            )
        certificate = self._platform_certificate()
        if serial.upper() != format(certificate.serial_number, "X"):
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED",
                "WeChat Pay signed the response with an unknown certificate.",
            )
        try:
            signed_at = int(timestamp)
        except ValueError as exc:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED",
                "WeChat Pay response timestamp is not an integer.",
            ) from exc
        if abs(time.time() - signed_at) > _RESPONSE_SKEW_SECONDS:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED",
                "WeChat Pay response signature is outside the accepted window.",
            )
        message = f"{timestamp}\n{nonce}\n{response.content.decode('utf-8')}\n".encode()
        try:
            certificate.public_key().verify(
                base64.b64decode(signature),
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED",
                "WeChat Pay response signature does not verify.",
            ) from exc

    def _signed_request(
        self,
        method: str,
        path: str,
        query: str,
        body: dict | None,
    ) -> dict:
        mchid = self._resolve("mchid") or ""
        merchant_serial = self._resolve("serial_no") or ""
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        body_bytes = body_text.encode("utf-8")

        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        target = f"{path}?{query}" if query else path
        message = f"{method}\n{target}\n{timestamp}\n{nonce}\n{body_text}\n"

        private_key = self._load_private_key()
        signature = base64.b64encode(
            private_key.sign(
                message.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        ).decode("utf-8")

        headers = {
            "Accept": "application/json",
            "Authorization": (
                f'WECHATPAY2-SHA256-RSA2048 mchid="{mchid}",'
                f'nonce_str="{nonce}",timestamp="{timestamp}",'
                f'serial_no="{merchant_serial}",signature="{signature}"'
            ),
        }
        if body_bytes:
            headers["Content-Type"] = "application/json"

        response = send_with_retry(
            self._client_instance(),
            method,
            f"{self._base_url()}{target}",
            headers=headers,
            content=body_bytes,
        )
        if response.status_code >= 400:
            upstream_code = ""
            try:
                upstream_code = str((response.json() or {}).get("code", ""))
            except ValueError:
                upstream_code = ""
            error = PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR",
                f"WeChat Pay returned HTTP {response.status_code}.",
            )
            error.upstream_code = upstream_code
            raise error
        if not response.content:
            return {}
        self._verify_response(response)
        try:
            return response.json()
        except ValueError as exc:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR",
                "WeChat Pay returned an unparsable response.",
            ) from exc

    # ------------------------------------------------------------------ orders

    def create_payment(self, order) -> dict:
        appid = self._resolve("appid") or ""
        mchid = self._resolve("mchid") or ""
        notify_url = self._resolve("notify_url") or ""
        payload = {
            "appid": appid,
            "mchid": mchid,
            "description": f"Order {order.id} - {order.plan.code}",
            "out_trade_no": str(order.id),
            "notify_url": notify_url,
            "amount": {"total": order.amount_cents, "currency": order.currency},
        }
        data = self._signed_request("POST", "/v3/pay/transactions/native", "", payload)
        return {
            "provider": "wechat_v3",
            "trade_no": str(order.id),
            "prepay_id": data.get("prepay_id", ""),
            "code_url": data.get("code_url", ""),
        }

    def query_order(self, order) -> ProviderOrderState:
        out_trade_no = str(order.id)
        mchid = self._resolve("mchid") or ""
        data = self._signed_request(
            "GET",
            f"/v3/pay/transactions/out-trade-no/{out_trade_no}",
            f"mchid={mchid}",
            None,
        )
        amount = data.get("amount") or {}
        return ProviderOrderState(
            status=_TRADE_STATE_MAP.get(data.get("trade_state", ""), "unknown"),
            amount_cents=amount.get("total"),
        )

    def refund(
        self,
        order,
        reason: str,
        amount_cents: int | None = None,
        refund_reference: str = "",
    ) -> dict:
        refund_cents = amount_cents if amount_cents is not None else order.amount_cents
        # The caller owns the refund key: one key per slice, so a retry maps to
        # the same provider refund while a second partial refund stays distinct.
        out_refund_no = refund_reference or f"refund-{order.id}"
        payload = {
            "out_trade_no": str(order.id),
            "out_refund_no": out_refund_no,
            "reason": reason[:200],
            "amount": {
                "refund": refund_cents,
                "total": order.amount_cents,
                "currency": order.currency,
            },
        }
        data = self._signed_request("POST", "/v3/refund/domestic/refunds", "", payload)
        return {
            "provider": "wechat_v3",
            "refund_id": data.get("refund_id", ""),
            "status": data.get("status", ""),
            "out_refund_no": payload["out_refund_no"],
        }

    def close_payment(self, order) -> dict:
        mchid = self._resolve("mchid") or ""
        try:
            data = self._signed_request(
                "POST",
                f"/v3/pay/transactions/out-trade-no/{order.id}/close",
                "",
                {"mchid": mchid},
            )
        except PaymentProviderError as exc:
            # Already closed or already settled elsewhere: closing is idempotent
            # from our point of view, and the caller re-queries the trade state.
            if getattr(exc, "upstream_code", "") in {"ORDER_CLOSED", "ORDER_NOT_EXIST"}:
                return {"provider": "wechat_v3", "closed": True, "already": True}
            raise
        return {
            "provider": "wechat_v3",
            "closed": True,
            "status": data.get("trade_state", "CLOSED"),
        }

    def query_refund(self, order, refund_reference: str) -> dict:
        data = self._signed_request(
            "GET", f"/v3/refund/domestic/refunds/{refund_reference}", "", None
        )
        amount = data.get("amount") or {}
        return {
            "provider": "wechat_v3",
            "status": _REFUND_STATUS_MAP.get(data.get("status", ""), "unknown"),
            "provider_status": data.get("status", ""),
            "refund_cents": amount.get("refund"),
            "refund_id": data.get("refund_id", ""),
        }

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

        api_v3_key = self._resolve("api_v3_key")
        if not api_v3_key:
            return CallbackResult(ok=False, message="api_v3 key not configured")

        try:
            certificate = self._platform_certificate()
        except Exception as exc:  # noqa: BLE001 - fail closed on bad cert material
            return CallbackResult(ok=False, message=f"platform certificate invalid: {exc}")

        trusted_serial = format(certificate.serial_number, "x")
        header_serial = str(headers["Wechatpay-Serial"]).strip().lower()
        if header_serial != trusted_serial:
            return CallbackResult(ok=False, message="untrusted platform certificate serial")

        timestamp = headers["Wechatpay-Timestamp"]
        nonce = headers["Wechatpay-Nonce"]
        try:
            signature = base64.b64decode(headers["Wechatpay-Signature"])
        except ValueError as exc:
            return CallbackResult(ok=False, message=f"signature decode failed: {exc}")

        message = f"{timestamp}\n{nonce}\n".encode() + body + b"\n"
        public_key = certificate.public_key()
        try:
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
        except Exception as exc:  # noqa: BLE001 - any parse/decrypt failure fails closed
            return CallbackResult(ok=False, message=f"resource decode failed: {exc}")

        event_type = str(envelope.get("event_type", ""))
        if event_type.startswith("REFUND."):
            return self._parse_refund_notify(plaintext)

        amount = plaintext.get("amount") or {}
        amount_total = amount.get("total")
        return CallbackResult(
            ok=True,
            status=_TRADE_STATE_MAP.get(plaintext.get("trade_state", ""), "unknown"),
            order_reference=plaintext.get("out_trade_no"),
            raw=plaintext,
            amount_cents=amount_total,
        )

    @staticmethod
    def _parse_refund_notify(plaintext: dict) -> CallbackResult:
        """Map a decrypted refund notify resource onto a callback result.

        The provider refund number (``out_refund_no``, falling back to
        ``refund_id``) is carried as ``refund_reference`` so the reclaim
        ledger can bind one row to one refund. A missing refund reference is
        left ``None`` rather than fabricated; the view degrades to a
        deterministic local reference derived from the order and slice.
        """

        amount = plaintext.get("amount") or {}
        refund_reference = plaintext.get("out_refund_no") or plaintext.get("refund_id") or None
        return CallbackResult(
            ok=True,
            status=_REFUND_STATUS_MAP.get(plaintext.get("refund_status", ""), "refund_abnormal"),
            order_reference=plaintext.get("out_trade_no"),
            refund_reference=refund_reference,
            raw=plaintext,
            amount_cents=amount.get("refund"),
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
