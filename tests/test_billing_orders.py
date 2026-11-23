import json

import pytest
from core.billing_models import EntitlementGrant, Order, Plan
from core.models import Tenant, TenantMembership, TenantTokenBudget
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.fixture
def orders_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="orders-owner")
    member = user_model.objects.create_user(username="orders-member")
    tenant = Tenant.objects.create(name="Orders tenant", slug="orders-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    plan = Plan.objects.create(
        code="pro-monthly",
        name="Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_daily=10_000,
        token_quota_monthly=50_000,
        features=["chat"],
    )
    return owner, member, tenant, plan


def _order_url(tenant_id, suffix="orders"):
    return f"/api/v1/tenants/{tenant_id}/billing/{suffix}/"


def _create_order(client, tenant, plan, key, channel="wechat"):
    return client.post(
        _order_url(tenant.id, "orders"),
        {"plan_code": plan.code, "channel": channel, "idempotency_key": key},
        format="json",
    )


def _callback(client, tenant, trade_no, status, signature="fake-ok", channel="wechat"):
    body = json.dumps({"out_trade_no": trade_no, "status": status})
    return client.post(
        f"/api/v1/tenants/{tenant.id}/billing/payments/{channel}/callback/",
        data=body,
        content_type="application/json",
        HTTP_X_FAKE_SIGNATURE=signature,
    )


@pytest.mark.django_db
def test_order_create_fails_when_no_provider(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = _create_order(client, tenant, plan, "no-provider-1")

    assert response.status_code == 409
    assert response.data["error"]["code"] == "PAYMENT_PROVIDER_NOT_CONFIGURED"


@pytest.mark.django_db
def test_purchase_channel_not_supported(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _order_url(tenant.id, "orders"),
        {"plan_code": plan.code, "channel": "paypal", "idempotency_key": "channel-1"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "PAYMENT_CHANNEL_NOT_SUPPORTED"


@pytest.mark.django_db
def test_billing_requires_admin_for_order_create(orders_setup):
    _, member, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=member)

    response = _create_order(client, tenant, plan, "member-1")

    assert response.status_code == 403


@pytest.mark.django_db
def test_billing_cross_tenant_isolation(orders_setup):
    owner, _, tenant, plan = orders_setup
    user_model = get_user_model()
    foreign_user = user_model.objects.create_user(username="orders-foreign")
    foreign_tenant = Tenant.objects.create(name="Foreign", slug="orders-foreign")
    TenantMembership.objects.create(tenant=foreign_tenant, user=foreign_user, role="owner")
    foreign_order = Order.objects.create(
        tenant=foreign_tenant,
        plan=plan,
        channel="wechat",
        idempotency_key="foreign-1",
        created_by=foreign_user,
        provider_trade_no="fake-foreign",
        status=Order.Status.PENDING,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    listing = client.get(_order_url(tenant.id, "orders"))
    assert listing.status_code == 200
    assert str(foreign_order.id) not in [item["id"] for item in listing.data["results"]]

    detail = client.get(_order_url(tenant.id, f"orders/{foreign_order.id}"))
    assert detail.status_code == 404


@pytest.mark.django_db
def test_order_state_machine_rejects_backwards(orders_setup):
    owner, _, tenant, plan = orders_setup
    order = Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel="wechat",
        idempotency_key="state-machine-1",
        created_by=owner,
        status=Order.Status.CREATED,
    )

    with pytest.raises(ValueError, match="cannot transition"):
        order.transition(Order.Status.PAID)

    order.transition(Order.Status.PENDING)
    order.transition(Order.Status.PAID)

    with pytest.raises(ValueError, match="cannot transition"):
        order.transition(Order.Status.CLOSED)
    with pytest.raises(ValueError, match="cannot transition"):
        order.transition(Order.Status.CREATED)


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_duplicate_callback_applied_once(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "dup-1")
    assert created.status_code == 201
    trade_no = created.data["payment"]["trade_no"]

    first = _callback(client, tenant, trade_no, "paid")
    second = _callback(client, tenant, trade_no, "paid")

    assert first.status_code == 200
    assert second.status_code == 200
    order = Order.objects.get(id=created.data["id"])
    assert order.status == Order.Status.PAID
    assert EntitlementGrant.objects.filter(order=order).count() == 1


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_out_of_order_callback_does_not_regress(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "ooo-1")
    trade_no = created.data["payment"]["trade_no"]

    _callback(client, tenant, trade_no, "paid")
    late = _callback(client, tenant, trade_no, "closed")

    assert late.status_code == 200
    order = Order.objects.get(id=created.data["id"])
    assert order.status == Order.Status.PAID


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_paid_order_grants_entitlement_once(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "grant-1")
    trade_no = created.data["payment"]["trade_no"]
    _callback(client, tenant, trade_no, "paid")
    _callback(client, tenant, trade_no, "paid")

    order = Order.objects.get(id=created.data["id"])
    grants = EntitlementGrant.objects.filter(order=order)
    assert grants.count() == 1
    assert grants.first().tokens == plan.token_quota_monthly


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_grant_updates_tenant_budget_via_ledger(orders_setup):
    owner, _, tenant, plan = orders_setup
    budget = TenantTokenBudget.objects.create(
        tenant=tenant, daily_limit=10_000, monthly_limit=1_000_000
    )
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "ledger-1")
    trade_no = created.data["payment"]["trade_no"]
    _callback(client, tenant, trade_no, "paid")

    budget.refresh_from_db()
    assert budget.monthly_limit == 1_000_000 + plan.token_quota_monthly

    _callback(client, tenant, trade_no, "paid")
    budget.refresh_from_db()
    assert budget.monthly_limit == 1_000_000 + plan.token_quota_monthly


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_checkout_instructions_are_persisted_and_replayed_on_detail(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "instruction-1")
    assert created.status_code == 201
    order = Order.objects.get(id=created.data["id"])
    assert order.payment_instruction == created.data["payment"]
    assert order.payment_instruction["code_url"].startswith("weixin://")

    detail = client.get(_order_url(tenant.id, f"orders/{order.id}"))
    assert detail.status_code == 200
    assert detail.data["payment"] == created.data["payment"]
    order.refresh_from_db()
    assert order.payment_instruction == created.data["payment"]


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_status_poll_reports_local_state_and_leaves_it_untouched(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = _create_order(client, tenant, plan, "poll-1")
    order = Order.objects.get(id=created.data["id"])

    pending = client.get(_order_url(tenant.id, f"orders/{order.id}/status"))
    assert pending.status_code == 200
    assert pending.data["status"] == Order.Status.PENDING
    assert pending.data["provider_status"] == "unknown"
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING

    trade_no = created.data["payment"]["trade_no"]
    assert _callback(client, tenant, trade_no, "paid").status_code == 200
    settled = client.get(_order_url(tenant.id, f"orders/{order.id}/status"))
    assert settled.data["status"] == Order.Status.PAID
    assert settled.data["paid_at"] is not None


@override_settings(BILLING_FAKE_PROVIDER_ENABLED=True)
@pytest.mark.django_db
def test_unknown_trade_callback_gets_a_json_envelope(orders_setup):
    owner, _, tenant, plan = orders_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = _callback(client, tenant, "trade-that-never-existed", "paid")

    assert response.status_code == 404
    assert response.data["error"]["code"] == "ORDER_NOT_FOUND"
    assert response["Content-Type"].startswith("application/json")
