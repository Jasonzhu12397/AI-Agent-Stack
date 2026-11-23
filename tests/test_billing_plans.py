import pytest
from core.billing_models import Plan
from core.models import AuditEvent, ModelProvider, Tenant, TenantMembership, TenantTokenBudget
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def billing_plans_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="plans-owner")
    tenant = Tenant.objects.create(name="Plans tenant", slug="plans-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    plan = Plan.objects.create(
        code="pro-monthly",
        name="Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_daily=10_000,
        token_quota_monthly=50_000,
        features=["chat"],
    )
    return owner, tenant, plan


def _provider(tenant, user, region, name):
    return ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name=name,
        provider_type="openai_compatible",
        deployment_region=region,
    )


def _url(tenant_id, path):
    return f"/api/v1/tenants/{tenant_id}/billing/{path}/"


@pytest.mark.django_db
def test_billing_capabilities_without_provider(billing_plans_setup):
    owner, tenant, _ = billing_plans_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(_url(tenant.id, "capabilities"))

    assert response.status_code == 200
    assert response.data["payment"]["available"] is False
    assert response.data["payment"]["reason_code"] == "PAYMENT_PROVIDER_NOT_CONFIGURED"
    assert response.data["payment"]["channels"] == []


@pytest.mark.django_db
def test_purchase_region_not_supported_and_audited(billing_plans_setup):
    owner, tenant, plan = billing_plans_setup
    _provider(tenant, owner, "overseas", "overseas-provider")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _url(tenant.id, "orders"),
        {"plan_code": plan.code, "channel": "wechat", "idempotency_key": "region-1"},
        format="json",
    )

    assert response.status_code == 422
    assert response.data["error"]["code"] == "PURCHASE_REGION_NOT_SUPPORTED"
    assert AuditEvent.objects.filter(
        tenant=tenant,
        event_type="billing.purchase.region_denied",
        outcome=AuditEvent.Outcome.DENIED,
    ).exists()


@pytest.mark.django_db
def test_purchase_region_unclassified_fails_closed(billing_plans_setup):
    owner, tenant, plan = billing_plans_setup
    _provider(tenant, owner, "", "unclassified-provider")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _url(tenant.id, "orders"),
        {"plan_code": plan.code, "channel": "wechat", "idempotency_key": "region-2"},
        format="json",
    )

    assert response.status_code == 422
    assert response.data["error"]["code"] == "PURCHASE_REGION_NOT_SUPPORTED"


@pytest.mark.django_db
def test_usage_endpoint_matches_token_budget(billing_plans_setup):
    owner, tenant, _ = billing_plans_setup
    TenantTokenBudget.objects.create(tenant=tenant, daily_limit=200, monthly_limit=400)
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(_url(tenant.id, "usage"))

    assert response.status_code == 200
    assert response.data["daily_limit"] == 200
    assert response.data["monthly_limit"] == 400
    assert "daily_used" in response.data
    assert "monthly_used" in response.data
