"""Payment channel readiness must be explainable without leaking a secret.

An operator who deploys the billing channel with merchant keys in the process
environment or a secret store needs to know *why* checkout is unavailable. The
answer must be reproducible from the provider interface itself: every adapter
declares the credential slots it needs, so readiness, the missing-slot list and
the registry filter all derive from one declaration instead of each adapter
repeating its own ``is_configured`` tuple.

Covered invariants:

* ``is_configured`` is derived from declared required slots for every real
  channel, and an optional slot missing does not disable the channel;
* the capabilities endpoint reports a per-channel diagnostic listing only slot
  names, never a reference URI or a secret value;
* a channel whose slots do not resolve stays out of the registry while the
  diagnostic still reports it, so "unavailable" always has a named cause;
* the ops command reports the same readiness and fails loudly when no channel
  is usable.
"""

import pytest
from core.models import Tenant, TenantMembership
from core.payments.alipay import AlipayProvider
from core.payments.registry import build_providers, diagnose_channels, provider_instances
from core.payments.wechat_v3 import WechatPayV3Provider
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

SECRET_SENTINEL = "sentinel-merchant-secret-value"

WECHAT_ENV = {
    "WECHATPAY_APPID": "wx-appid-test",
    "WECHATPAY_MCHID": "1600000000",
    "WECHATPAY_API_V3_KEY": SECRET_SENTINEL,
    "WECHATPAY_CERT_SERIAL_NO": "merchant-serial-001",
    "WECHATPAY_PRIVATE_KEY": SECRET_SENTINEL,
    "WECHATPAY_PLATFORM_CERT": SECRET_SENTINEL,
}

ALIPAY_ENV = {
    "ALIPAY_APP_ID": "alipay-app-test",
    "ALIPAY_PRIVATE_KEY": SECRET_SENTINEL,
    "ALIPAY_PUBLIC_KEY": SECRET_SENTINEL,
}


@pytest.fixture
def clean_payment_env(monkeypatch):
    for name in (
        *WECHAT_ENV,
        *ALIPAY_ENV,
        "WECHATPAY_NOTIFY_URL",
        "WECHATPAY_BASE_URL",
        "ALIPAY_NOTIFY_URL",
        "ALIPAY_GATEWAY",
        "BILLING_FAKE_PROVIDER_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tenant_with_owner(db):
    owner = get_user_model().objects.create_user(username="diag-owner")
    tenant = Tenant.objects.create(name="Diagnostics tenant", slug="diagnostics-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    return owner, tenant


def _set_env(monkeypatch, mapping):
    for name, value in mapping.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    "provider_class,mapping",
    [(WechatPayV3Provider, WECHAT_ENV), (AlipayProvider, ALIPAY_ENV)],
)
def test_configuration_is_derived_from_declared_required_slots(
    settings, clean_payment_env, monkeypatch, provider_class, mapping
):
    provider = provider_class()

    assert provider.is_configured() is False
    assert provider.missing_slots() == list(provider_class.REQUIRED_SLOTS)

    _set_env(monkeypatch, mapping)
    assert provider.is_configured() is True
    assert provider.missing_slots() == []


@pytest.mark.parametrize(
    "provider_class,mapping,slot,variable",
    [
        (WechatPayV3Provider, WECHAT_ENV, "api_v3_key", "WECHATPAY_API_V3_KEY"),
        (AlipayProvider, ALIPAY_ENV, "public_key", "ALIPAY_PUBLIC_KEY"),
    ],
)
def test_one_missing_required_slot_disables_the_channel(
    settings,
    clean_payment_env,
    monkeypatch,
    provider_class,
    mapping,
    slot,
    variable,
):
    env = dict(mapping)
    env.pop(variable)
    _set_env(monkeypatch, env)

    provider = provider_class()

    assert provider.is_configured() is False
    assert provider.missing_slots() == [slot]


def test_optional_slot_absence_keeps_the_channel_usable(
    settings, clean_payment_env, monkeypatch
):
    _set_env(monkeypatch, WECHAT_ENV)

    provider = WechatPayV3Provider()

    assert "notify_url" in provider.SLOTS
    assert "notify_url" not in provider.REQUIRED_SLOTS
    assert provider.is_configured() is True


def test_diagnostics_report_unconfigured_channels_by_slot_name_only(
    settings, clean_payment_env, monkeypatch
):
    _set_env(monkeypatch, WECHAT_ENV)
    monkeypatch.setenv("WECHATPAY_API_V3_KEY", "")

    entries = {entry["channel"]: entry for entry in diagnose_channels()}

    assert set(entries) == {"wechat", "alipay"}
    assert entries["wechat"]["configured"] is False
    assert entries["wechat"]["missing"] == ["api_v3_key"]
    assert entries["alipay"]["missing"] == list(AlipayProvider.REQUIRED_SLOTS)
    rendered = repr(entries)
    assert SECRET_SENTINEL not in rendered
    assert "env://" not in rendered
    assert "vault://" not in rendered


def test_registry_keeps_unconfigured_channels_out(settings, clean_payment_env):
    assert build_providers() == {}
    assert set(provider_instances()) == {"wechat", "alipay"}


@pytest.mark.django_db
def test_capabilities_endpoint_explains_missing_slots(
    settings, clean_payment_env, tenant_with_owner
):
    owner, tenant = tenant_with_owner
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(f"/api/v1/tenants/{tenant.id}/billing/capabilities/")

    assert response.status_code == 200
    payment = response.data["payment"]
    assert payment["available"] is False
    diagnostics = {entry["channel"]: entry for entry in payment["diagnostics"]}
    assert diagnostics["wechat"]["missing"] == list(WechatPayV3Provider.REQUIRED_SLOTS)
    body = str(response.data)
    assert "env://" not in body
    assert SECRET_SENTINEL not in body


@pytest.mark.django_db
def test_capabilities_endpoint_reports_configured_channel(
    settings, clean_payment_env, monkeypatch, tenant_with_owner
):
    owner, tenant = tenant_with_owner
    _set_env(monkeypatch, WECHAT_ENV)
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(f"/api/v1/tenants/{tenant.id}/billing/capabilities/")

    payment = response.data["payment"]
    assert payment["available"] is True
    assert payment["channels"] == ["wechat"]
    diagnostics = {entry["channel"]: entry for entry in payment["diagnostics"]}
    assert diagnostics["wechat"]["configured"] is True
    assert diagnostics["alipay"]["configured"] is False


@pytest.mark.django_db
def test_check_payment_channels_command_fails_when_no_channel_is_usable(
    settings, clean_payment_env
):
    with pytest.raises(CommandError) as error:
        call_command("check_payment_channels")

    assert "wechat" in str(error.value)
    assert "PAYMENT_PROVIDER_NOT_CONFIGURED" in str(error.value)
