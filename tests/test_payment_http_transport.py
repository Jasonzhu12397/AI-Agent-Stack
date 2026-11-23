"""Offline HTTP-transport and callback security tests for real payment adapters.

No real merchant or outbound network connection is involved: every provider
call goes through an ``httpx.MockTransport`` that mounts official-shaped
payloads. RSA key pairs and the platform certificate are generated at runtime
and injected through ``env://`` references; secrets are resolved server-side
and must never appear in responses, receipts or audit metadata.

Covered invariants:

* WeChat v3: real ``WECHATPAY2-SHA256-RSA2048`` request signing, platform
  certificate serial trust check, AES-256-GCM resource decryption;
* Alipay: RSA2 sorted-content request signing and async-notify verification;
* repeated callbacks apply once, out-of-order callbacks never regress state;
* a callback amount that does not match the order is rejected and audited;
* no private key, symmetric key, certificate or PEM material leaks into any
  response, receipt or audit record.
"""

import base64
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from core.billing_models import (
    EntitlementGrant,
    EntitlementReclaim,
    Order,
    PaymentTransactionAudit,
    Plan,
)
from core.models import AuditEvent, Tenant, TenantMembership, TenantTokenBudget
from core.payments.alipay import AlipayProvider
from core.payments.wechat_v3 import WechatPayV3Provider
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

# --------------------------------------------------------------------------- #
# Cryptographic fixtures and payload builders
# --------------------------------------------------------------------------- #


def _rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    return private, private_pem


def _self_signed_cert(private_key) -> x509.Certificate:
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "pay-test-platform")]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )


def _encrypt_resource(
    plaintext: dict, api_v3_key: str, associated_data: str = "transaction"
) -> dict:
    aesgcm = AESGCM(api_v3_key.encode("utf-8"))
    nonce = os.urandom(12).hex()
    plaintext_bytes = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    ciphertext = aesgcm.encrypt(
        nonce.encode("utf-8"), plaintext_bytes, associated_data.encode("utf-8")
    )
    return {
        "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
        "nonce": nonce,
        "associated_data": associated_data,
    }


def _wechat_notify(
    cert: x509.Certificate,
    platform_private,
    api_v3_key: str,
    out_trade_no: str,
    amount_cents: int,
    trade_state: str = "SUCCESS",
    *,
    serial_override: str | None = None,
    body_mutator=None,
    key_override: str | None = None,
):
    plaintext = {
        "out_trade_no": out_trade_no,
        "trade_state": trade_state,
        "amount": {"total": amount_cents, "currency": "CNY"},
    }
    resource = _encrypt_resource(plaintext, key_override or api_v3_key)
    envelope = {"id": "evt-1", "event_type": "TRANSACTION.SUCCESS", "resource": resource}
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = f"{timestamp}\n{nonce}\n".encode() + body + b"\n"
    signature = base64.b64encode(
        platform_private.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("utf-8")
    if body_mutator is not None:
        body = body_mutator(body)
    serial = serial_override or format(cert.serial_number, "x")
    headers = {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": serial,
        "Wechatpay-Signature": signature,
    }
    return headers, body


def _wechat_refund_notify(
    cert: x509.Certificate,
    platform_private,
    api_v3_key: str,
    out_trade_no: str,
    out_refund_no: str,
    refund_cents: int,
    total_cents: int,
    refund_status: str = "SUCCESS",
    *,
    event_type: str = "REFUND.SUCCESS",
    serial_override: str | None = None,
    body_mutator=None,
):
    plaintext = {
        "out_trade_no": out_trade_no,
        "out_refund_no": out_refund_no,
        "refund_id": f"refund-{out_refund_no}",
        "refund_status": refund_status,
        "amount": {"refund": refund_cents, "total": total_cents, "currency": "CNY"},
    }
    resource = _encrypt_resource(plaintext, api_v3_key, associated_data="refund")
    envelope = {"id": "evt-refund-1", "event_type": event_type, "resource": resource}
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = f"{timestamp}\n{nonce}\n".encode() + body + b"\n"
    signature = base64.b64encode(
        platform_private.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("utf-8")
    if body_mutator is not None:
        body = body_mutator(body)
    serial = serial_override or format(cert.serial_number, "x")
    headers = {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": serial,
        "Wechatpay-Signature": signature,
    }
    return headers, body


def _header_kwargs(headers: dict) -> dict:
    return {
        f"HTTP_{name.upper().replace('-', '_')}": value
        for name, value in headers.items()
    }


@pytest.fixture
def wechat_env(monkeypatch):
    merchant_private, merchant_pem = _rsa_keypair()
    platform_private, _ = _rsa_keypair()
    platform_cert = _self_signed_cert(platform_private)
    platform_cert_pem = platform_cert.public_bytes(
        serialization.Encoding.PEM
    ).decode("utf-8")
    api_v3_key = "0123456789abcdef0123456789abcdef"

    monkeypatch.setenv("WECHATPAY_APPID", "wx-appid-test")
    monkeypatch.setenv("WECHATPAY_MCHID", "1600000000")
    monkeypatch.setenv("WECHATPAY_API_V3_KEY", api_v3_key)
    monkeypatch.setenv("WECHATPAY_CERT_SERIAL_NO", "merchant-serial-001")
    monkeypatch.setenv("WECHATPAY_PRIVATE_KEY", merchant_pem)
    monkeypatch.setenv("WECHATPAY_PLATFORM_CERT", platform_cert_pem)
    monkeypatch.setenv("WECHATPAY_NOTIFY_URL", "https://example.test/notify")
    monkeypatch.setenv("WECHATPAY_BASE_URL", "https://pay.test.local")

    return {
        "merchant_private": merchant_private,
        "merchant_public": merchant_private.public_key(),
        "platform_private": platform_private,
        "platform_cert": platform_cert,
        "api_v3_key": api_v3_key,
        "merchant_pem": merchant_pem,
        "platform_cert_pem": platform_cert_pem,
    }


@pytest.fixture
def billing_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="pay-http-owner")
    tenant = Tenant.objects.create(name="Pay HTTP tenant", slug="pay-http-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    plan = Plan.objects.create(
        code="pro-monthly",
        name="Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_daily=10_000,
        token_quota_monthly=50_000,
    )
    return owner, tenant, plan


def _pending_order(tenant, plan, owner, amount_cents=9900, channel="wechat") -> Order:
    order = Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel=channel,
        idempotency_key=str(uuid.uuid4()),
        created_by=owner,
        status=Order.Status.PENDING,
        amount_cents=amount_cents,
    )
    order.provider_trade_no = str(order.id)
    order.save(update_fields=["provider_trade_no"])
    return order


def _callback_url(tenant_id, channel="wechat"):
    return f"/api/v1/tenants/{tenant_id}/billing/payments/{channel}/callback/"


def _wechat_mock_handler(captured: dict, merchant_public):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        auth = request.headers.get("authorization", "")
        captured["authorization"] = auth

        prefix = "WECHATPAY2-SHA256-RSA2048 "
        assert auth.startswith(prefix)
        fields = {}
        for item in auth[len(prefix) :].split(","):
            key, value = item.strip().split("=", 1)
            fields[key] = value.strip('"')
        signature = base64.b64decode(fields["signature"])
        body_text = request.content.decode("utf-8")
        message = (
            f"{request.method}\n{request.url.path}\n"
            f"{fields['timestamp']}\n{fields['nonce_str']}\n{body_text}\n"
        )
        merchant_public.verify(
            signature, message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )

        if "/native" in request.url.path:
            return httpx.Response(
                200, json={"prepay_id": "prepay-123", "code_url": "weixin://wxpay/bizpayurl?pr=abc"}
            )
        if "/refunds" in request.url.path:
            return httpx.Response(200, json={"refund_id": "refund-1", "status": "SUCCESS"})
        return httpx.Response(200, json={"trade_state": "SUCCESS"})

    return handler


# --------------------------------------------------------------------------- #
# WeChat Pay v3 outbound requests
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_wechat_native_create_sends_signed_request_and_returns_instructions(wechat_env):
    captured: dict = {}
    transport = httpx.MockTransport(_wechat_mock_handler(captured, wechat_env["merchant_public"]))
    provider = WechatPayV3Provider(transport=transport)
    order = SimpleNamespace(
        id="order-123",
        plan=SimpleNamespace(code="pro-monthly"),
        amount_cents=9900,
        currency="CNY",
    )

    instructions = provider.create_payment(order)

    assert instructions["provider"] == "wechat_v3"
    assert instructions["trade_no"] == "order-123"
    assert instructions["prepay_id"] == "prepay-123"
    assert instructions["code_url"].startswith("weixin://")
    assert captured["method"] == "POST"
    assert "/v3/pay/transactions/native" in captured["url"]

    body = json.loads(captured["body"])
    assert body["out_trade_no"] == "order-123"
    assert body["amount"]["total"] == 9900
    assert body["amount"]["currency"] == "CNY"

    serialized = json.dumps(instructions) + captured["body"] + captured["authorization"]
    assert "PRIVATE KEY" not in serialized
    assert wechat_env["api_v3_key"] not in serialized


@pytest.mark.django_db
def test_wechat_refund_request_payload_is_correct(wechat_env):
    captured: dict = {}
    transport = httpx.MockTransport(_wechat_mock_handler(captured, wechat_env["merchant_public"]))
    provider = WechatPayV3Provider(transport=transport)
    order = SimpleNamespace(
        id="order-123",
        plan=SimpleNamespace(code="pro-monthly"),
        amount_cents=9900,
        currency="CNY",
    )

    receipt = provider.refund(order, "customer requested refund")

    body = json.loads(captured["body"])
    assert body["out_trade_no"] == "order-123"
    assert body["out_refund_no"] == "refund-order-123"
    assert body["reason"] == "customer requested refund"
    assert body["amount"] == {"refund": 9900, "total": 9900, "currency": "CNY"}
    assert receipt["refund_id"] == "refund-1"
    assert receipt["status"] == "SUCCESS"


# --------------------------------------------------------------------------- #
# WeChat Pay v3 callback invariants
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_wechat_callback_paid_grants_entitlement_once_on_repeat(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    budget = TenantTokenBudget.objects.get_or_create(tenant=tenant)[0]
    baseline_limit = budget.monthly_limit
    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
    )
    client = APIClient()

    first = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )
    assert first.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert EntitlementGrant.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline_limit + plan.token_quota_monthly

    second = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )
    assert second.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    # Repeated callback: entitlement credited exactly once, budget unchanged.
    assert EntitlementGrant.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline_limit + plan.token_quota_monthly


@pytest.mark.django_db
def test_wechat_callback_signature_invalid_rejected_and_audited(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)

    def _tamper(body: bytes) -> bytes:
        envelope = json.loads(body)
        envelope["resource"]["nonce"] = "0" * 24
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
        body_mutator=_tamper,
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_SIGNATURE_INVALID"
    assert AuditEvent.objects.filter(
        event_type="billing.callback.signature_invalid",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
    assert PaymentTransactionAudit.objects.filter(
        event_type="payment.callback.rejected",
        error_code="PAYMENT_SIGNATURE_INVALID",
    ).exists()
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_wechat_callback_amount_mismatch_rejected_and_audited(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents + 100,  # callback amount differs from the order
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_AMOUNT_MISMATCH"
    details = response.data["error"]["details"]
    assert details["expected_cents"] == order.amount_cents
    assert details["actual_cents"] == order.amount_cents + 100
    assert AuditEvent.objects.filter(
        event_type="billing.callback.amount_mismatch",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
    assert PaymentTransactionAudit.objects.filter(
        event_type="payment.callback.amount_mismatch",
        error_code="PAYMENT_AMOUNT_MISMATCH",
    ).exists()
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_wechat_callback_untrusted_serial_rejected(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
        serial_override="deadbeefcafe",
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_SIGNATURE_INVALID"
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_wechat_callback_wrong_api_v3_key_fails_decryption(wechat_env, monkeypatch):
    good_key = wechat_env["api_v3_key"]
    other_key = "ffffffffffffffffffffffffffffffff"
    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        good_key,
        "order-123",
        9900,
        key_override=good_key,
    )
    monkeypatch.setenv("WECHATPAY_API_V3_KEY", other_key)

    provider = WechatPayV3Provider()
    result = provider.verify_callback(headers, body)

    assert result.ok is False
    assert "decode" in result.message.lower() or "invalid" in result.message.lower()


@pytest.mark.django_db
def test_wechat_out_of_order_callback_does_not_regress(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    client = APIClient()

    paid_headers, paid_body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
        trade_state="SUCCESS",
    )
    paid = client.post(
        _callback_url(tenant.id),
        data=paid_body,
        content_type="application/json",
        **_header_kwargs(paid_headers),
    )
    assert paid.status_code == 200

    closed_headers, closed_body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
        trade_state="CLOSED",
    )
    late = client.post(
        _callback_url(tenant.id),
        data=closed_body,
        content_type="application/json",
        **_header_kwargs(closed_headers),
    )
    assert late.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert AuditEvent.objects.filter(
        event_type="billing.callback.out_of_order",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()


@pytest.mark.django_db
def test_payment_records_never_leak_key_material(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    headers, body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )
    assert response.status_code == 200

    dumps = [json.dumps(response.data)]
    for event in AuditEvent.objects.all():
        dumps.append(json.dumps(event.metadata, default=str))
    for row in PaymentTransactionAudit.objects.all():
        dumps.append(json.dumps(row.metadata, default=str))
        dumps.append(row.error_code)
    blob = "\n".join(dumps)

    assert "BEGIN PRIVATE KEY" not in blob
    assert "BEGIN CERTIFICATE" not in blob
    assert wechat_env["api_v3_key"] not in blob
    assert wechat_env["merchant_pem"] not in blob
    assert wechat_env["platform_cert_pem"] not in blob


@pytest.mark.django_db
def test_wechat_refund_notification_reclaims_once_on_replay(wechat_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    budget = TenantTokenBudget.objects.get_or_create(tenant=tenant)[0]
    client = APIClient()

    pay_headers, pay_body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
    )
    paid = client.post(
        _callback_url(tenant.id),
        data=pay_body,
        content_type="application/json",
        **_header_kwargs(pay_headers),
    )
    assert paid.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    budget.refresh_from_db()
    post_payment_limit = budget.monthly_limit

    refund_headers, refund_body = _wechat_refund_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        "out-refund-once",
        order.amount_cents,
        order.amount_cents,
    )
    first = client.post(
        _callback_url(tenant.id),
        data=refund_body,
        content_type="application/json",
        **_header_kwargs(refund_headers),
    )
    second = client.post(
        _callback_url(tenant.id),
        data=refund_body,
        content_type="application/json",
        **_header_kwargs(refund_headers),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert EntitlementReclaim.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == post_payment_limit - plan.token_quota_monthly


@pytest.mark.django_db
def test_wechat_refund_notification_invalid_signature_is_fail_closed(
    wechat_env, billing_setup
):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    budget = TenantTokenBudget.objects.get_or_create(tenant=tenant)[0]
    baseline = budget.monthly_limit

    def _tamper(body: bytes) -> bytes:
        envelope = json.loads(body)
        envelope["resource"]["nonce"] = "0" * 24
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    headers, body = _wechat_refund_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        "out-refund-bad-sig",
        order.amount_cents,
        order.amount_cents,
        body_mutator=_tamper,
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        **_header_kwargs(headers),
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_SIGNATURE_INVALID"
    assert AuditEvent.objects.filter(
        event_type="billing.callback.signature_invalid",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline
    assert EntitlementReclaim.objects.filter(order=order).count() == 0


@pytest.mark.django_db
def test_wechat_refund_notification_does_not_reissue_entitlement(
    wechat_env, billing_setup
):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner)
    budget = TenantTokenBudget.objects.get_or_create(tenant=tenant)[0]
    client = APIClient()

    pay_headers, pay_body = _wechat_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        order.amount_cents,
    )
    client.post(
        _callback_url(tenant.id),
        data=pay_body,
        content_type="application/json",
        **_header_kwargs(pay_headers),
    )
    order.refresh_from_db()
    grant_count = EntitlementGrant.objects.filter(order=order).count()
    budget.refresh_from_db()
    limit_after_payment = budget.monthly_limit

    refund_headers, refund_body = _wechat_refund_notify(
        wechat_env["platform_cert"],
        wechat_env["platform_private"],
        wechat_env["api_v3_key"],
        str(order.id),
        "out-refund-no-regrant",
        order.amount_cents,
        order.amount_cents,
    )
    refund = client.post(
        _callback_url(tenant.id),
        data=refund_body,
        content_type="application/json",
        **_header_kwargs(refund_headers),
    )

    assert refund.status_code == 200
    assert EntitlementGrant.objects.filter(order=order).count() == grant_count
    budget.refresh_from_db()
    assert budget.monthly_limit <= limit_after_payment
    assert EntitlementReclaim.objects.filter(order=order).count() == 1


# --------------------------------------------------------------------------- #
# Alipay outbound requests and callbacks
# --------------------------------------------------------------------------- #


def _alipay_signed_content(params: dict[str, str]) -> str:
    excluded = {"sign", "sign_type"}
    items = sorted(
        (key, value)
        for key, value in params.items()
        if key not in excluded and value != ""
    )
    return "&".join(f"{key}={value}" for key, value in items)


def _alipay_verify(params: dict[str, str], public_key) -> bool:
    try:
        public_key.verify(
            base64.b64decode(params["sign"]),
            _alipay_signed_content(params).encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:  # noqa: BLE001 - any verify failure is invalid
        return False


@pytest.fixture
def alipay_env(monkeypatch):
    private, private_pem = _rsa_keypair()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("ALIPAY_APP_ID", "alipay-app-test")
    monkeypatch.setenv("ALIPAY_PRIVATE_KEY", private_pem)
    monkeypatch.setenv("ALIPAY_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("ALIPAY_NOTIFY_URL", "https://example.test/alipay/notify")
    monkeypatch.setenv("ALIPAY_GATEWAY", "https://alipay.test.local/gateway.do")
    return {
        "private": private,
        "public_key": private.public_key(),
        "private_pem": private_pem,
        "public_pem": public_pem,
    }


def _alipay_notify(
    private_key,
    out_trade_no: str,
    amount_cents: int,
    trade_status: str = "TRADE_SUCCESS",
    *,
    amount_yuan: str | None = None,
    tamper_amount: str | None = None,
) -> bytes:
    params = {
        "app_id": "alipay-app-test",
        "out_trade_no": out_trade_no,
        "trade_status": trade_status,
        "total_amount": amount_yuan or f"{amount_cents / 100:.2f}",
        "notify_id": "notify-1",
        "notify_time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "sign_type": "RSA2",
    }
    content = _alipay_signed_content(params)
    sign = base64.b64encode(
        private_key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    params["sign"] = sign
    if tamper_amount is not None:
        params["total_amount"] = tamper_amount
    return urlencode(params).encode()


def _alipay_mock_handler(captured: dict, public_key):
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        captured["form"] = form
        assert _alipay_verify(form, public_key)
        biz = json.loads(form["biz_content"])
        method = form["method"]
        if method == "alipay.trade.precreate":
            return httpx.Response(
                200,
                json={
                    "alipay_trade_precreate_response": {
                        "code": "10000",
                        "msg": "Success",
                        "qr_code": "qr://alipay/123",
                        "out_trade_no": biz["out_trade_no"],
                    },
                    "sign": form["sign"],
                },
            )
        if method == "alipay.trade.refund":
            return httpx.Response(
                200,
                json={
                    "alipay_trade_refund_response": {
                        "code": "10000",
                        "trade_status": "TRADE_SUCCESS",
                        "refund_fee": biz["refund_amount"],
                        "trade_no": "alipay-trade-1",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "alipay_trade_query_response": {
                    "code": "10000",
                    "trade_status": "TRADE_SUCCESS",
                }
            },
        )

    return handler


@pytest.mark.django_db
def test_alipay_precreate_sends_signed_form_and_returns_qr(alipay_env):
    captured: dict = {}
    transport = httpx.MockTransport(
        _alipay_mock_handler(captured, alipay_env["public_key"])
    )
    provider = AlipayProvider(transport=transport)
    order = SimpleNamespace(
        id="order-456",
        plan=SimpleNamespace(code="pro-monthly"),
        amount_cents=9900,
        currency="CNY",
    )

    instructions = provider.create_payment(order)

    assert instructions["provider"] == "alipay"
    assert instructions["trade_no"] == "order-456"
    assert instructions["qr_code"] == "qr://alipay/123"
    form = captured["form"]
    assert form["method"] == "alipay.trade.precreate"
    biz = json.loads(form["biz_content"])
    assert biz["out_trade_no"] == "order-456"
    assert biz["total_amount"] == "99.00"
    assert form["sign"]

    serialized = json.dumps(instructions) + urlencode(form)
    assert "PRIVATE KEY" not in serialized
    assert alipay_env["private_pem"] not in serialized


@pytest.mark.django_db
def test_alipay_refund_request_payload_is_correct(alipay_env):
    captured: dict = {}
    transport = httpx.MockTransport(
        _alipay_mock_handler(captured, alipay_env["public_key"])
    )
    provider = AlipayProvider(transport=transport)
    order = SimpleNamespace(
        id="order-456",
        plan=SimpleNamespace(code="pro-monthly"),
        amount_cents=9900,
        currency="CNY",
    )

    receipt = provider.refund(order, "customer requested refund")

    biz = json.loads(captured["form"]["biz_content"])
    assert biz["out_trade_no"] == "order-456"
    assert biz["refund_amount"] == "99.00"
    assert biz["refund_reason"] == "customer requested refund"
    assert receipt["refund_id"] == "alipay-trade-1"


@pytest.mark.django_db
def test_alipay_callback_paid_grants_entitlement_once_on_repeat(alipay_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner, channel="alipay")
    budget = TenantTokenBudget.objects.get_or_create(tenant=tenant)[0]
    baseline_limit = budget.monthly_limit
    body = _alipay_notify(alipay_env["private"], str(order.id), order.amount_cents)
    client = APIClient()

    first = client.post(
        _callback_url(tenant.id, "alipay"),
        data=body,
        content_type="application/x-www-form-urlencoded",
    )
    assert first.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert EntitlementGrant.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline_limit + plan.token_quota_monthly

    second = client.post(
        _callback_url(tenant.id, "alipay"),
        data=body,
        content_type="application/x-www-form-urlencoded",
    )
    assert second.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert EntitlementGrant.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline_limit + plan.token_quota_monthly


@pytest.mark.django_db
def test_alipay_callback_signature_invalid_rejected_and_audited(alipay_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner, channel="alipay")
    body = _alipay_notify(
        alipay_env["private"],
        str(order.id),
        order.amount_cents,
        tamper_amount="88.00",
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id, "alipay"),
        data=body,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_SIGNATURE_INVALID"
    assert AuditEvent.objects.filter(
        event_type="billing.callback.signature_invalid",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
    assert PaymentTransactionAudit.objects.filter(
        event_type="payment.callback.rejected",
        error_code="PAYMENT_SIGNATURE_INVALID",
    ).exists()
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_alipay_callback_amount_mismatch_rejected_and_audited(alipay_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner, channel="alipay")
    body = _alipay_notify(
        alipay_env["private"],
        str(order.id),
        order.amount_cents,
        amount_yuan="100.00",
    )
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id, "alipay"),
        data=body,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_AMOUNT_MISMATCH"
    details = response.data["error"]["details"]
    assert details["expected_cents"] == order.amount_cents
    assert details["actual_cents"] == 10_000
    assert AuditEvent.objects.filter(
        event_type="billing.callback.amount_mismatch",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
    assert PaymentTransactionAudit.objects.filter(
        event_type="payment.callback.amount_mismatch",
        error_code="PAYMENT_AMOUNT_MISMATCH",
    ).exists()
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_alipay_callback_untrusted_public_key_rejected(alipay_env, monkeypatch):
    other_private, _ = _rsa_keypair()
    other_public_pem = other_private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("ALIPAY_PUBLIC_KEY", other_public_pem)

    body = _alipay_notify(alipay_env["private"], "order-456", 9900)
    provider = AlipayProvider()
    result = provider.verify_callback({}, body)

    assert result.ok is False
    assert "RSA2" in result.message


@pytest.mark.django_db
def test_alipay_out_of_order_callback_does_not_regress(alipay_env, billing_setup):
    owner, tenant, plan = billing_setup
    order = _pending_order(tenant, plan, owner, channel="alipay")
    client = APIClient()

    paid_body = _alipay_notify(
        alipay_env["private"], str(order.id), order.amount_cents
    )
    paid = client.post(
        _callback_url(tenant.id, "alipay"),
        data=paid_body,
        content_type="application/x-www-form-urlencoded",
    )
    assert paid.status_code == 200

    closed_body = _alipay_notify(
        alipay_env["private"],
        str(order.id),
        order.amount_cents,
        trade_status="TRADE_CLOSED",
    )
    late = client.post(
        _callback_url(tenant.id, "alipay"),
        data=closed_body,
        content_type="application/x-www-form-urlencoded",
    )
    assert late.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert AuditEvent.objects.filter(
        event_type="billing.callback.out_of_order",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()
