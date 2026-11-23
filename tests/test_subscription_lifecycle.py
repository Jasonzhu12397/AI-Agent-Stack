"""Subscription lifecycle contract tests: trial, cancel, renew, expiry.

Covers the state machine, the HTTP endpoints, ledger/audit pairing, and the
"no overuse after expiry" proof: once a subscription settles to expired the
budget is reset to the plan's post-expiry fallback quota, a further reservation
is rejected by the token guard, and the settled usage totals do not grow.
"""

from datetime import timedelta

import pytest
from core.billing_models import EntitlementGrant, Order, Plan, Subscription
from core.models import (
    AuditEvent,
    Tenant,
    TenantMembership,
    TenantTokenBudget,
    TenantTokenUsage,
)
from core.subscription_service import (
    SubscriptionStateError,
    cancel_subscription,
    settle_due_subscriptions,
    start_trial,
)
from core.token_budget import TenantTokenBudgetExceeded, get_tenant_token_usage
from core.token_guard import AdHocTokenGuard
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def lifecycle_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="life-owner")
    member = user_model.objects.create_user(username="life-member")
    stranger = user_model.objects.create_user(username="life-stranger")
    tenant = Tenant.objects.create(name="Life tenant", slug="life-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    plan_trial = Plan.objects.create(
        code="trial-pro",
        name="Trial Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_daily=10_000,
        token_quota_monthly=50_000,
        token_quota_daily_expired=0,
        token_quota_monthly_expired=0,
        trial_days=7,
        features=["chat"],
    )
    plan_no_trial = Plan.objects.create(
        code="stable-basic",
        name="Stable Basic",
        interval="monthly",
        price_cents=0,
        token_quota_daily=1_000,
        token_quota_monthly=5_000,
        trial_days=0,
        features=[],
    )
    return owner, member, stranger, tenant, plan_trial, plan_no_trial


def _billing_url(tenant_id, suffix):
    return f"/api/v1/tenants/{tenant_id}/billing/{suffix.strip('/')}/"


def _paid_order(tenant, plan, owner, key):
    now = timezone.now()
    return Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel="wechat",
        idempotency_key=key,
        created_by=owner,
        status=Order.Status.PAID,
        paid_at=now,
    )


def _active_subscription(tenant, plan):
    now = timezone.now()
    return Subscription.objects.create(
        tenant=tenant,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        started_at=now,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )


@pytest.mark.django_db
def test_trial_is_rejected_when_plan_has_no_trial(lifecycle_setup):
    owner, _, _, tenant, _, plan_no_trial = lifecycle_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _billing_url(tenant.id, "subscription/trial/"),
        {"plan_code": plan_no_trial.code},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "SUBSCRIPTION_PLAN_NO_TRIAL"
    assert Subscription.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_trial_starts_and_settles_to_expired(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    created = client.post(
        _billing_url(tenant.id, "subscription/trial/"),
        {"plan_code": plan.code},
        format="json",
    )

    assert created.status_code == 201
    body = created.data["subscription"]
    assert body["status"] == Subscription.Status.TRIALING
    assert body["current_period_start"]
    assert body["current_period_end"]

    subscription = Subscription.objects.get(tenant=tenant, plan=plan)
    assert subscription.status == Subscription.Status.TRIALING
    assert subscription.current_period_end - subscription.current_period_start == timedelta(days=7)

    budget = TenantTokenBudget.objects.get(tenant=tenant)
    assert budget.daily_limit == plan.token_quota_daily
    assert budget.monthly_limit == plan.token_quota_monthly

    settle_due_subscriptions(now=timezone.now() + timedelta(days=8))
    subscription.refresh_from_db()
    assert subscription.status == Subscription.Status.EXPIRED
    assert AuditEvent.objects.filter(
        tenant=tenant, event_type="billing.subscription.expired"
    ).exists()


@pytest.mark.django_db
def test_renewal_extends_period_and_grants_quota_once(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    subscription = _active_subscription(tenant, plan)
    order = _paid_order(tenant, plan, owner, "life-renew-1")
    client = APIClient()
    client.force_authenticate(user=owner)

    first = client.post(
        _billing_url(tenant.id, "subscription/renew/"),
        {"order_id": str(order.id)},
        format="json",
    )

    assert first.status_code == 200
    renewed = Subscription.objects.get(pk=subscription.pk)
    assert renewed.status == Subscription.Status.ACTIVE
    assert renewed.current_period_end > subscription.current_period_end

    grants = EntitlementGrant.objects.filter(subscription=subscription)
    assert grants.count() == 1
    assert grants.first().tokens == plan.token_quota_monthly

    budget = TenantTokenBudget.objects.get(tenant=tenant)
    assert budget.daily_limit == plan.token_quota_daily
    assert budget.monthly_limit == plan.token_quota_monthly

    # A second renewal inside the same period must not stack another grant.
    second = client.post(
        _billing_url(tenant.id, "subscription/renew/"),
        {"order_id": str(order.id)},
        format="json",
    )
    assert second.status_code == 409
    assert second.data["error"]["code"] == "SUBSCRIPTION_GRANT_ALREADY_ISSUED"
    assert EntitlementGrant.objects.filter(subscription=subscription).count() == 1


@pytest.mark.django_db
def test_period_end_cancel_stays_active_then_settles_cancelled(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    subscription = _active_subscription(tenant, plan)
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _billing_url(tenant.id, "subscription/cancel/"),
        {"mode": "period_end"},
        format="json",
    )

    assert response.status_code == 200
    body = response.data["subscription"]
    assert body["status"] == Subscription.Status.ACTIVE
    assert body["cancel_at_period_end"] is True
    assert body["cancel_effective_at"] == subscription.current_period_end.isoformat()

    settle_due_subscriptions(now=timezone.now() + timedelta(days=31))
    subscription.refresh_from_db()
    assert subscription.status == Subscription.Status.CANCELLED
    # A period-end cancellation must never land in ``expired``.
    assert subscription.status != Subscription.Status.EXPIRED
    assert AuditEvent.objects.filter(
        tenant=tenant, event_type="billing.subscription.cancelled_at_period_end"
    ).exists()


@pytest.mark.django_db
def test_immediate_cancel_and_invalid_transition_are_rejected(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    expired = Subscription.objects.create(
        tenant=tenant,
        plan=plan,
        status=Subscription.Status.EXPIRED,
        started_at=timezone.now(),
    )

    with pytest.raises(SubscriptionStateError) as exc:
        cancel_subscription(subscription=expired, mode="immediate")
    assert exc.value.code == "SUBSCRIPTION_INVALID_TRANSITION"
    assert exc.value.http_status == 409

    with pytest.raises(ValueError, match="cannot transition"):
        expired.transition(Subscription.Status.ACTIVE)

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(
        _billing_url(tenant.id, "subscription/cancel/"),
        {"mode": "immediate"},
        format="json",
    )
    assert response.status_code == 409
    assert response.data["error"]["code"] == "SUBSCRIPTION_INVALID_TRANSITION"


@pytest.mark.django_db
def test_no_overuse_after_expiry(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    started = client.post(
        _billing_url(tenant.id, "subscription/trial/"),
        {"plan_code": plan.code},
        format="json",
    )
    assert started.status_code == 201

    now = timezone.now()
    TenantTokenUsage.objects.create(
        tenant=tenant,
        idempotency_key="life-settled-1",
        scope_ref="model-test:m-1",
        status=TenantTokenUsage.Status.SETTLED,
        reserved_tokens=0,
        consumed_tokens=9_900,
        expires_at=now,
        settled_at=now,
    )
    usage_before = get_tenant_token_usage(tenant.id)
    assert usage_before["daily_used"] == 9_900
    assert usage_before["monthly_used"] == 9_900

    settle_due_subscriptions(now=now + timedelta(days=8))

    # Assertion 1: the budget limit equals the plan's post-expiry rule (zero),
    # not a carry-over of the paid quota.
    budget = TenantTokenBudget.objects.get(tenant=tenant)
    assert budget.daily_limit == plan.token_quota_daily_expired == 0
    assert budget.monthly_limit == plan.token_quota_monthly_expired == 0

    # Assertion 2: a further provider-call reservation is rejected by the
    # token guard before any provider request is issued.
    guard = AdHocTokenGuard(tenant=tenant, scope_ref="model-test:m-2")
    with pytest.raises(TenantTokenBudgetExceeded):
        guard.reserve(idempotency_key="life-reserve-1", requested_tokens=100)

    # Assertion 3: settled usage totals did not grow after expiry.
    usage_after = get_tenant_token_usage(tenant.id)
    assert usage_after["daily_used"] == usage_before["daily_used"] == 9_900
    assert usage_after["monthly_used"] == usage_before["monthly_used"] == 9_900


@pytest.mark.django_db
def test_cross_tenant_isolation_and_anonymous_rejection(lifecycle_setup):
    owner, member, stranger, tenant, plan, _ = lifecycle_setup
    user_model = get_user_model()
    foreign_user = user_model.objects.create_user(username="life-foreign")
    foreign_tenant = Tenant.objects.create(name="Foreign life", slug="life-foreign")
    TenantMembership.objects.create(
        tenant=foreign_tenant, user=foreign_user, role="owner"
    )
    foreign_subscription = _active_subscription(foreign_tenant, plan)

    client = APIClient()
    client.force_authenticate(user=owner)
    listing = client.get(_billing_url(tenant.id, "subscription/"))
    assert listing.status_code == 200
    assert listing.data["subscription"] is None
    assert str(foreign_subscription.id) not in str(listing.data)

    # Non-member gets a 404 envelope, never the foreign tenant's record.
    stranger_client = APIClient()
    stranger_client.force_authenticate(user=stranger)
    assert stranger_client.get(_billing_url(tenant.id, "subscription/")).status_code == 404

    # Member (non-admin) cannot start a trial.
    member_client = APIClient()
    member_client.force_authenticate(user=member)
    forbidden = member_client.post(
        _billing_url(tenant.id, "subscription/trial/"),
        {"plan_code": plan.code},
        format="json",
    )
    assert forbidden.status_code == 403

    # Anonymous callers are rejected with the contract envelope.
    anonymous = APIClient()
    rejected = anonymous.post(
        _billing_url(tenant.id, "subscription/trial/"),
        {"plan_code": plan.code},
        format="json",
    )
    assert rejected.status_code == 401
    assert rejected.data["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert {"code", "message", "details"} <= set(rejected.data["error"])


@pytest.mark.django_db
def test_service_start_trial_rejects_second_active_subscription(lifecycle_setup):
    owner, _, _, tenant, plan, _ = lifecycle_setup
    start_trial(tenant=tenant, plan=plan)

    with pytest.raises(SubscriptionStateError) as exc:
        start_trial(tenant=tenant, plan=plan)
    assert exc.value.code == "SUBSCRIPTION_ALREADY_ACTIVE"
    assert exc.value.http_status == 409
