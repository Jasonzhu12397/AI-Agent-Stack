"""Tenant notifications to IM group robots.

DingTalk and Feishu group robots require a signed request: the signature covers
``timestamp + "\\n" + secret`` (base64 of the HMAC-SHA256 digest), and the same
keyword must appear in the signed string. WeCom group robots carry their key in
the URL and need no signature. Every adapter is fail-closed: an unsigned request
is never sent, and a non-2xx or non-zero ``errcode`` is reported back to the
caller so the console can show why a channel is not delivering.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core.field_crypto import open_sealed

_SCOPE = "notification-endpoint"
_TIMEOUT = 8.0
# Group robots throttle at 20 messages/minute; one retry on 5xx is plenty.
_RETRIES = 1


class NotificationChannelError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    message: str = ""
    status_code: int | None = None


def resolve_url(endpoint) -> str:
    value = open_sealed(endpoint.sealed_url, _SCOPE)
    if not value:
        raise NotificationChannelError(
            "ENDPOINT_UNREADABLE", "The stored webhook could not be read."
        )
    return value


def resolve_secret(endpoint) -> str:
    return open_sealed(endpoint.sealed_secret, _SCOPE) or ""


def dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    """DingTalk custom-robot signature: base64(HMAC-SHA256(secret, ts + '\\n' + secret))."""

    payload = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def feishu_sign(secret: str, timestamp_s: str) -> str:
    """Feishu custom-bot signature: base64(HMAC-SHA256(key=ts + '\\n' + secret, msg=''))."""

    key = f"{timestamp_s}\n{secret}".encode()
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def build_request(endpoint, title: str, body: str, *, timestamp_ms: str | None = None):
    """Return ``(url, payload)`` for one endpoint, signing where required."""

    url = resolve_url(endpoint)
    secret = resolve_secret(endpoint)
    text = f"### {title}\n\n{body}" if endpoint.channel != "feishu" else f"{title}\n{body}"

    if endpoint.channel == "dingtalk":
        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        if secret:
            stamp = timestamp_ms or str(int(time.time() * 1000))
            sign = dingtalk_sign(secret, stamp)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}timestamp={stamp}&sign={sign}"
        return url, payload

    if endpoint.channel == "feishu":
        stamp = str(int(time.time()))
        payload = {"msg_type": "text", "content": {"text": text}}
        if secret:
            payload["timestamp"] = stamp
            payload["sign"] = feishu_sign(secret, stamp)
        return url, payload

    # WeCom (企业微信群机器人): the key lives in the URL, no signature.
    return url, {"msgtype": "markdown", "markdown": {"content": text}}


def _success(channel: str, payload: dict[str, Any]) -> tuple[bool, str]:
    code = payload.get("errcode", payload.get("code", 0))
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        numeric = 0
    if numeric not in (0, 200):
        reason = payload.get("errmsg") or payload.get("msg") or code
        return False, f"{channel} rejected the message: {reason}"
    return True, ""


def deliver(
    endpoint,
    title: str,
    body: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timestamp_ms: str | None = None,
) -> DeliveryResult:
    """Send one notification; transport is injectable so tests stay offline."""

    url, payload = build_request(endpoint, title, body, timestamp_ms=timestamp_ms)
    last: DeliveryResult = DeliveryResult(ok=False, message="not attempted")
    for attempt in range(_RETRIES + 1):
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
                response = client.post(url, json=payload)
            if response.status_code >= 500 and attempt < _RETRIES:
                last = DeliveryResult(
                    False, f"upstream {response.status_code}", response.status_code
                )
                continue
            if response.status_code >= 400:
                return DeliveryResult(
                    False, f"upstream {response.status_code}", response.status_code
                )
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
            ok, message = _success(endpoint.channel, decoded)
            return DeliveryResult(ok, message, response.status_code)
        except httpx.HTTPError as exc:
            last = DeliveryResult(False, f"transport error: {type(exc).__name__}")
    return last
