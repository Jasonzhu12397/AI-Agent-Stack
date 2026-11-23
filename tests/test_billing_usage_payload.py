"""HTTP contract tests for ``/billing/usage/`` payload.

The usage history must come from the real settled token-usage ledger; no value
may be estimated, copied from the daily/monthly snapshot, or fabricated when
the ledger is empty.
"""

import re
from datetime import datetime, timedelta

import pytest
from core.models import (
    Tenant,
    TenantMembership,
    TenantTokenBudget,
    TenantTokenUsage,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def usage_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="usage-owner")
    stranger = user_model.objects.create_user(username="usage-stranger")
    tenant = Tenant.objects.create(name="Usage tenant", slug="usage-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantTokenBudget.objects.create(
        tenant=tenant, daily_limit=100_000, monthly_limit=1_000_000
    )
    return owner, stranger, tenant


def _usage_url(tenant_id):
    return f"/api/v1/tenants/{tenant_id}/billing/usage/"


def _settle(tenant, *, key, scope_ref, consumed, settled_at):
    return TenantTokenUsage.objects.create(
        tenant=tenant,
        idempotency_key=key,
        scope_ref=scope_ref,
        status=TenantTokenUsage.Status.SETTLED,
        reserved_tokens=0,
        consumed_tokens=consumed,
        expires_at=settled_at,
        settled_at=settled_at,
    )


@pytest.mark.django_db
def test_usage_payload_empty_ledger_returns_empty_history_and_day_bounds(usage_setup):
    owner, _, tenant = usage_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    body = response.data
    assert body["history"] == []
    assert body["daily_used"] == 0
    assert body["monthly_used"] == 0
    assert body["period_start"]
    assert body["period_end"]
    start = datetime.fromisoformat(body["period_start"])
    end = datetime.fromisoformat(body["period_end"])
    assert end - start == timedelta(days=1)
    # Entitlement grants remain available under their own ledger field.
    assert "grants" in body


@pytest.mark.django_db
def test_usage_history_groups_settled_consumption_by_period_and_source(usage_setup):
    owner, _, tenant = usage_setup
    now = timezone.now()
    previous_month = now - timedelta(days=40)
    _settle(
        tenant,
        key="usage-ingest-1",
        scope_ref="rag-ingest:doc-7",
        consumed=120,
        settled_at=now,
    )
    _settle(
        tenant,
        key="usage-retrieve-1",
        scope_ref="rag-retrieve:proj-3",
        consumed=80,
        settled_at=now,
    )
    _settle(
        tenant,
        key="usage-model-test-1",
        scope_ref="model-test:m-9",
        consumed=45,
        settled_at=previous_month,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    body = response.data
    history = body["history"]
    assert len(history) == 3
    for row in history:
        assert set(row.keys()) == {"period", "consumed_tokens", "source"}
        assert re.fullmatch(r"\d{4}-\d{2}", row["period"])

    grouped = {(row["period"], row["source"]): row["consumed_tokens"] for row in history}
    current_period = timezone.localtime(now).strftime("%Y-%m")
    previous_period = timezone.localtime(previous_month).strftime("%Y-%m")
    assert grouped[(current_period, "rag-ingest:doc-7")] == 120
    assert grouped[(current_period, "rag-retrieve:proj-3")] == 80
    assert grouped[(previous_period, "model-test:m-9")] == 45

    current_month_consumed = sum(
        consumed
        for (period, _source), consumed in grouped.items()
        if period == current_period
    )
    assert current_month_consumed == body["monthly_used"] == 200
    assert body["daily_used"] == 200


@pytest.mark.django_db
def test_usage_payload_remaining_equals_limit_minus_used(usage_setup):
    owner, _, tenant = usage_setup
    now = timezone.now()
    _settle(
        tenant,
        key="usage-rem-under-1",
        scope_ref="rag-ingest:doc-1",
        consumed=20_000,
        settled_at=now,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    body = response.data
    assert body["daily_used"] == 20_000
    assert body["monthly_used"] == 20_000
    assert body["remaining_daily"] == 80_000
    assert body["remaining_monthly"] == 980_000
    assert body["remaining_daily"] >= 0
    assert body["remaining_monthly"] >= 0


@pytest.mark.django_db
def test_usage_payload_remaining_floors_at_zero_when_usage_exceeds_limit(usage_setup):
    owner, _, tenant = usage_setup
    budget = TenantTokenBudget.objects.get(tenant=tenant)
    budget.monthly_limit = 100_000
    budget.save(update_fields=["monthly_limit", "updated_at"])
    now = timezone.now()
    _settle(
        tenant,
        key="usage-rem-over-1",
        scope_ref="rag-retrieve:proj-1",
        consumed=120_000,
        settled_at=now,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    body = response.data
    assert body["daily_used"] == 120_000
    assert body["monthly_used"] == 120_000
    # Overspent ledger rows must never project a negative remaining quota.
    assert body["remaining_daily"] == 0
    assert body["remaining_monthly"] == 0


@pytest.mark.django_db
def test_usage_payload_exposes_reset_times_and_limits_source(usage_setup):
    owner, _, tenant = usage_setup
    now = timezone.now()

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    body = response.data
    # Daily reset shares the exact local day boundary with ``period_end``.
    assert body["reset_at"] == body["period_end"]
    reset_at = datetime.fromisoformat(body["reset_at"])
    period_end = datetime.fromisoformat(body["period_end"])
    assert reset_at == period_end

    month_reset = datetime.fromisoformat(body["monthly_reset_at"])
    local_now = timezone.localtime(now)
    expected_month_reset = local_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if expected_month_reset.month == 12:
        expected_month_reset = expected_month_reset.replace(
            year=expected_month_reset.year + 1, month=1
        )
    else:
        expected_month_reset = expected_month_reset.replace(
            month=expected_month_reset.month + 1
        )
    assert month_reset == expected_month_reset

    assert body["limits_source"] == "tenant_budget"


@pytest.mark.django_db
def test_usage_history_ignores_released_and_unsettled_rows(usage_setup):
    owner, _, tenant = usage_setup
    now = timezone.now()
    TenantTokenUsage.objects.create(
        tenant=tenant,
        idempotency_key="usage-released-1",
        scope_ref="rag-retrieve:proj-1",
        status=TenantTokenUsage.Status.RELEASED,
        reserved_tokens=0,
        consumed_tokens=0,
        expires_at=now,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 200
    assert response.data["history"] == []


@pytest.mark.django_db
def test_usage_endpoint_rejects_anonymous_request(usage_setup):
    _, _, tenant = usage_setup
    client = APIClient()

    response = client.get(_usage_url(tenant.id))

    # Anonymous callers are rejected with 401 and the contract error envelope.
    assert response.status_code == 401
    assert "detail" not in response.data
    error = response.data["error"]
    assert error["code"] == "AUTHENTICATION_REQUIRED"
    assert {"code", "message", "details"} <= set(error)


@pytest.mark.django_db
def test_usage_endpoint_rejects_non_member(usage_setup):
    _, stranger, tenant = usage_setup
    client = APIClient()
    client.force_authenticate(user=stranger)

    response = client.get(_usage_url(tenant.id))

    assert response.status_code == 404
