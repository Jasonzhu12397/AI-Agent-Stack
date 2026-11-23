import json

import pytest
from core.billing_models import Order, Plan
from core.models import AuditEvent, Tenant, TenantMembership
from core.payments import registry
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.fixture
def payments_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="payments-owner")
    tenant = Tenant.objects.create(name="Payments tenant", slug="payments-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    plan = Plan.objects.create(
        code="pro-monthly",
        name="Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_monthly=50_000,
    )
    return owner, tenant, plan


def _callback_url(tenant_id, channel="wechat"):
    return f"/api/v1/tenants/{tenant_id}/billing/payments/{channel}/callback/"


@pytest.mark.django_db
def test_registry_is_empty_without_configured_provider():
    assert registry.build_providers() == {}
    assert registry.has_configured_provider() is False


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_fake_provider_only_active_under_explicit_override():
    providers = registry.build_providers()
    assert "wechat" in providers
    assert providers["wechat"].is_configured() is True


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_callback_signature_invalid_audited(payments_setup):
    owner, tenant, plan = payments_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = client.post(
        f"/api/v1/tenants/{tenant.id}/billing/orders/",
        {"plan_code": plan.code, "channel": "wechat", "idempotency_key": "sig-1"},
        format="json",
    )
    trade_no = created.data["payment"]["trade_no"]
    body = json.dumps({"out_trade_no": trade_no, "status": "paid"})

    response = client.post(
        _callback_url(tenant.id),
        data=body,
        content_type="application/json",
        HTTP_X_FAKE_SIGNATURE="wrong-signature",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_SIGNATURE_INVALID"
    assert AuditEvent.objects.filter(
        event_type="billing.callback.signature_invalid",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()

    order = Order.objects.get(id=created.data["id"])
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_callback_unknown_channel_rejected(payments_setup):
    _, tenant, _ = payments_setup
    client = APIClient()
    response = client.post(
        _callback_url(tenant.id, "paypal"),
        data=json.dumps({"status": "paid"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_CHANNEL_NOT_SUPPORTED"
