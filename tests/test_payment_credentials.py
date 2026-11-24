"""Merchant credentials entered from the console finish a payment channel."""

import pytest
from core.billing_models import PaymentChannelSecret, Plan
from core.models import Tenant, TenantMembership
from core.payments.alipay import AlipayProvider
from core.payments.base import resolve_credential_ref
from core.payments.registry import build_providers, diagnose_channels
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

CHANNEL = "alipay"
VALUES = {
    "app_id": "2021000000000001",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----",
    "public_key": "-----BEGIN PUBLIC KEY-----\npublic\n-----END PUBLIC KEY-----",
    "notify_url": "https://console.example.test/api/v1/tenants/t/billing/payments/alipay/callback/",
}


@pytest.fixture
def admin_tenant(db):
    user = get_user_model().objects.create_user(username="pay-admin", password="pw-pay-admin")
    tenant = Tenant.objects.create(name="Pay tenant", slug="pay-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    return user, tenant


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_credentials_are_sealed_at_rest(admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    response = client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["configured"] is True
    # The read model never carries a value, only which slots are filled.
    assert sorted(response.json()["filled"]) == sorted(VALUES)
    assert all("value" not in slot for slot in response.json()["filled"])

    stored = PaymentChannelSecret.objects.get(channel=CHANNEL, slot="app_id")
    assert VALUES["app_id"] not in stored.sealed_value
    assert "secret" not in stored.sealed_value


@pytest.mark.django_db
def test_saved_slots_make_the_channel_purchasable(admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    providers = build_providers()
    assert CHANNEL in providers
    assert providers[CHANNEL].is_configured()
    capabilities = client.get(f"/api/v1/tenants/{tenant.id}/billing/capabilities/").json()
    assert CHANNEL in capabilities["payment"]["channels"]
    diagnostics = {entry["channel"]: entry for entry in diagnose_channels()}
    assert diagnostics[CHANNEL]["missing"] == []


@pytest.mark.django_db
def test_clearing_a_slot_takes_the_channel_offline(admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": {"private_key": ""}},
        format="json",
    )
    assert PaymentChannelSecret.objects.filter(channel=CHANNEL, slot="private_key").count() == 0
    assert CHANNEL not in build_providers()


@pytest.mark.django_db
def test_unknown_slot_is_rejected(admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    response = client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": {"merchant_id": "x"}},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.django_db
def test_unsupported_channel_is_a_404(admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/paypal/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_non_admin_member_cannot_write(admin_tenant):
    user, tenant = admin_tenant
    member = get_user_model().objects.create_user(username="pay-member", password="pw-member")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    response = _client(member).put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(PAYMENT_CREDENTIAL_ENV_PREFIXES=("ALIPAY_",))
def test_environment_wins_over_the_console_value(monkeypatch, admin_tenant):
    user, tenant = admin_tenant
    client = _client(user)
    client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    provider = AlipayProvider()
    monkeypatch.setenv("ALIPAY_APP_ID", "env-app-id")
    assert provider.resolve_slot("app_id") == "env-app-id"
    assert resolve_credential_ref("env://ALIPAY_APP_ID") == "env-app-id"


@pytest.mark.django_db
def test_plans_stay_purchasable_only_with_the_channel(admin_tenant):
    user, tenant = admin_tenant
    Plan.objects.create(code="starter", name="Starter", price_cents=9900)
    client = _client(user)
    before = client.get(f"/api/v1/tenants/{tenant.id}/billing/plans/").json()
    assert before["results"][0]["purchasable"] is False
    client.put(
        f"/api/v1/tenants/{tenant.id}/billing/payments/credentials/{CHANNEL}/",
        {"values": VALUES},
        format="json",
    )
    after = client.get(f"/api/v1/tenants/{tenant.id}/billing/plans/").json()
    assert after["results"][0]["purchasable"] is True
