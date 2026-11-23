"""Idempotency proofs for the entitlement ledger.

A replay of a payment callback, or a re-entrant call into the credit path, must
never credit a tenant twice for one order. These tests attack the ledger from
three directions: plain re-entry, a concurrent double entry, and a direct
duplicate row insert that the database itself must refuse.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from core.billing_models import EntitlementGrant, Order, Plan, Subscription
from core.billing_views import _credit_entitlement
from core.models import Tenant, TenantTokenBudget
from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction


@pytest.fixture
def order_setup(db):
    user = get_user_model().objects.create_user(username="ledger-owner", password="pw")
    tenant = Tenant.objects.create(name="Ledger Tenant", slug="ledger-tenant")
    plan = Plan.objects.create(
        code="ledger-plan",
        name="Ledger Plan",
        price_cents=9900,
        currency="CNY",
        token_quota_monthly=500_000,
    )
    order = Order.objects.create(
        tenant=tenant,
        plan=plan,
        amount_cents=9900,
        currency="CNY",
        channel=Order.Channel.WECHAT,
        status=Order.Status.PAID,
        provider_trade_no="trade-ledger-1",
        idempotency_key="ledger-order-1",
        created_by=user,
    )
    TenantTokenBudget.objects.get_or_create(
        tenant=tenant, defaults={"daily_limit": 0, "monthly_limit": 0}
    )
    return tenant, plan, order


@pytest.mark.django_db
def test_reentrant_credit_returns_same_grant_and_credits_budget_once(order_setup):
    tenant, plan, order = order_setup
    budget = TenantTokenBudget.objects.get(tenant=tenant)
    baseline = budget.monthly_limit

    first = _credit_entitlement(order)
    second = _credit_entitlement(order)

    assert first.id == second.id
    assert EntitlementGrant.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline + plan.token_quota_monthly


@pytest.mark.django_db(transaction=True)
def test_concurrent_credit_entitlement_grants_once(order_setup):
    tenant, plan, order = order_setup
    budget = TenantTokenBudget.objects.get(tenant=tenant)
    baseline = budget.monthly_limit
    barrier = threading.Barrier(2)

    def credit():
        barrier.wait()
        # sqlite allows one writer at a time; back off instead of failing the
        # concurrency proof on a "database is locked" artifact.
        for attempt in range(10):
            try:
                return _credit_entitlement(order).id
            except (IntegrityError, OperationalError) as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.05 * (2 ** attempt))
        raise AssertionError("credit path never completed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(credit), pool.submit(credit)]]

    grants = EntitlementGrant.objects.filter(order=order)
    assert grants.count() == 1, "a concurrent re-entry must not create a second grant"
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline + plan.token_quota_monthly
    assert results[0] == results[1] or None in results


@pytest.mark.django_db
def test_duplicate_grant_row_is_refused_by_the_database(order_setup):
    tenant, plan, order = order_setup
    subscription = Subscription.objects.create(
        tenant=tenant, plan=plan, status=Subscription.Status.ACTIVE
    )
    EntitlementGrant.objects.create(
        tenant=tenant,
        subscription=subscription,
        order=order,
        period=EntitlementGrant.Period.MONTHLY,
        tokens=1,
        reason="first",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EntitlementGrant.objects.create(
                tenant=tenant,
                subscription=subscription,
                order=order,
                period=EntitlementGrant.Period.MONTHLY,
                tokens=1,
                reason="duplicate",
            )
