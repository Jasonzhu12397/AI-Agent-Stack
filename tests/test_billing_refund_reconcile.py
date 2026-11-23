"""Refund state machine, audited quota reclaim and reconciliation report.

TDD suite for the refund/reconciliation block:

* one shared order state machine drives both wechat and alipay refunds;
* every quota reclaim lands on an append-only ledger row and is idempotent per
  refund reference (active refund and replayed callback cannot double-deduct);
* the reconciliation endpoint is read-only, tenant-isolated and reports
  provider discrepancies without turning an unconfigured channel into a 503.
"""

import pytest
from core.billing_models import EntitlementGrant, EntitlementReclaim, Order, Plan
from core.billing_views import _credit_entitlement
from core.models import Tenant, TenantMembership, TenantTokenBudget
from core.payments import registry
from core.payments.base import (
    CallbackResult,
    PaymentProvider,
    PaymentProviderError,
    ProviderOrderState,
)
from core.refund_service import reclaim_for_refund
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def refund_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="refund-owner")
    tenant = Tenant.objects.create(name="Refund tenant", slug="refund-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    plan = Plan.objects.create(
        code="pro-monthly",
        name="Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_monthly=50_000,
        features=["chat"],
    )
    budget = TenantTokenBudget.objects.create(
        tenant=tenant, daily_limit=10_000, monthly_limit=1_000_000
    )
    return owner, tenant, plan, budget


def _make_paid_order(tenant, plan, owner, key, channel="wechat"):
    order = Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel=channel,
        idempotency_key=key,
        created_by=owner,
        status=Order.Status.PAID,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        provider_trade_no=f"fake-{key}",
        paid_at=timezone.now(),
    )
    _credit_entitlement(order)
    return order


class StubProvider(PaymentProvider):
    """Configurable test double standing in for a real channel adapter."""

    def __init__(self, channel, status="paid", amount_cents=9900):
        self.channel = channel
        self.query_status = status
        self.query_amount_cents = amount_cents
        self.refunds: list[tuple[str, int | None, str]] = []
        self.closed: list[str] = []
        self.queries = 0
        self.fail_query = False
        self.fail_close = False
        self.refund_query_status = "refunded"
        self.refund_query_cents: int | None = None
        self.refund_queries: list[str] = []
        self.fail_refund_query = False

    def is_configured(self) -> bool:
        return True

    def create_payment(self, order) -> dict:
        return {"provider": "stub", "trade_no": f"fake-{order.id}"}

    def verify_callback(self, headers, body: bytes) -> CallbackResult:
        return CallbackResult(ok=True, status="paid", order_reference="")

    def query_order(self, order) -> ProviderOrderState:
        self.queries += 1
        if self.fail_query:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR", "query failed"
            )
        return ProviderOrderState(
            status=self.query_status, amount_cents=self.query_amount_cents
        )

    def refund(
        self,
        order,
        reason: str,
        amount_cents: int | None = None,
        refund_reference: str = "",
    ) -> dict:
        self.refunds.append((str(order.id), amount_cents, refund_reference))
        return {
            "provider": "stub",
            "refund_id": f"refund-{order.id}-{amount_cents}",
            "refund_reference": refund_reference,
            "status": "SUCCESS",
        }

    def close_payment(self, order) -> dict:
        if self.fail_close:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR", "close failed"
            )
        self.closed.append(str(order.id))
        return {"provider": "stub", "closed": True}

    def query_refund(self, order, refund_reference: str) -> dict:
        self.refund_queries.append(refund_reference)
        if self.fail_refund_query:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UPSTREAM_ERROR", "refund query failed"
            )
        return {
            "provider": "stub",
            "status": self.refund_query_status,
            "refund_cents": self.refund_query_cents,
        }


def _install_providers(monkeypatch, providers: dict[str, StubProvider]) -> None:
    monkeypatch.setattr(registry, "get_provider", lambda channel: providers.get(channel))
    monkeypatch.setattr(registry, "build_providers", lambda: dict(providers))


def _refund_url(tenant_id, order_id):
    return f"/api/v1/tenants/{tenant_id}/billing/orders/{order_id}/refund/"


def _reconciliation_url(tenant_id, start=None, end=None):
    base = f"/api/v1/tenants/{tenant_id}/billing/reconciliation/"
    if start is None and end is None:
        return base
    return f"{base}?start={start}&end={end}"


def _today_window():
    day = timezone.localtime().date()
    return day.isoformat(), day.isoformat()


@pytest.mark.django_db
def test_full_refund_moves_order_to_refunded_and_reclaims_quota_once(
    refund_setup, monkeypatch
):
    owner, tenant, plan, budget = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    order = _make_paid_order(tenant, plan, owner, "full-refund-1")
    budget.refresh_from_db()
    baseline = budget.monthly_limit

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(_refund_url(tenant.id, order.id), {"reason": "customer"},
                           format="json")

    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED

    reclaims = EntitlementReclaim.objects.filter(order=order)
    assert reclaims.count() == 1
    reclaim = reclaims.first()
    assert reclaim.refund_cents == order.amount_cents
    assert reclaim.tokens == plan.token_quota_monthly

    budget.refresh_from_db()
    assert budget.monthly_limit == baseline - plan.token_quota_monthly


@pytest.mark.django_db
def test_partial_refund_marks_partial_and_reclaims_proportionally(
    refund_setup, monkeypatch
):
    owner, tenant, plan, budget = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    order = _make_paid_order(tenant, plan, owner, "partial-refund-1")
    budget.refresh_from_db()
    baseline = budget.monthly_limit
    client = APIClient()
    client.force_authenticate(user=owner)

    first = client.post(
        _refund_url(tenant.id, order.id),
        {"amount_cents": 3300, "reason": "partial one"},
        format="json",
    )
    assert first.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PARTIAL_REFUNDED

    first_reclaim = EntitlementReclaim.objects.get(order=order, refund_cents=3300)
    assert first_reclaim.tokens == 16_666
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline - 16_666

    second = client.post(
        _refund_url(tenant.id, order.id),
        {"amount_cents": 6600, "reason": "partial two"},
        format="json",
    )
    assert second.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED

    reclaims = EntitlementReclaim.objects.filter(order=order)
    assert reclaims.count() == 2
    second_reclaim = reclaims.get(refund_cents=6600)
    assert second_reclaim.tokens == 33_333
    budget.refresh_from_db()
    assert budget.monthly_limit == baseline - 49_999


@pytest.mark.django_db
def test_refund_replay_does_not_reclaim_twice(refund_setup, monkeypatch):
    owner, tenant, plan, budget = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    order = _make_paid_order(tenant, plan, owner, "replay-refund-1")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_refund_url(tenant.id, order.id), {"reason": "dup"},
                           format="json")
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED

    reclaim = EntitlementReclaim.objects.get(order=order)
    reference = reclaim.refund_reference
    budget_before = budget.monthly_limit

    # Replayed refund callback carrying the same refund reference must reuse the
    # existing reclaim row instead of deducting the budget a second time.
    replayed = reclaim_for_refund(
        order=order,
        refund_reference=reference,
        refund_cents=order.amount_cents,
        reason="replayed callback",
    )

    assert replayed.id == reclaim.id
    assert EntitlementReclaim.objects.filter(order=order).count() == 1
    budget.refresh_from_db()
    assert budget.monthly_limit == budget_before
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Order.Status.CREATED, Order.Status.PENDING])
def test_refund_requires_paid_order(refund_setup, monkeypatch, status):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    order = Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel="wechat",
        idempotency_key=f"refund-require-{status}",
        created_by=owner,
        status=status,
        amount_cents=plan.price_cents,
        provider_trade_no=f"fake-{status}",
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(_refund_url(tenant.id, order.id), {"reason": "x"},
                           format="json")

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ORDER_STATE_CONFLICT"
    order.refresh_from_db()
    assert order.status == status
    assert EntitlementReclaim.objects.filter(order=order).count() == 0


@pytest.mark.django_db
def test_refund_above_refundable_amount_is_rejected(refund_setup, monkeypatch):
    owner, tenant, plan, budget = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    order = _make_paid_order(tenant, plan, owner, "over-refund-1")
    budget.refresh_from_db()
    budget_before = budget.monthly_limit

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(
        _refund_url(tenant.id, order.id),
        {"amount_cents": order.amount_cents + 1, "reason": "too much"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "REFUND_AMOUNT_EXCEEDED"
    assert response.data["error"]["details"]["refundable_cents"] == order.amount_cents

    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    budget.refresh_from_db()
    assert budget.monthly_limit == budget_before
    assert EntitlementReclaim.objects.filter(order=order).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["wechat", "alipay"])
def test_wechat_and_alipay_share_the_same_refund_state_machine(channel):
    from core.payments.alipay import AlipayProvider
    from core.payments.wechat_v3 import WechatPayV3Provider

    provider_cls = {
        "wechat": WechatPayV3Provider,
        "alipay": AlipayProvider,
    }[channel]

    # Neither adapter may own a second refund state machine.
    assert not hasattr(provider_cls, "TRANSITIONS")
    assert Order.Status.PARTIAL_REFUNDED in Order.TRANSITIONS[Order.Status.PAID]
    assert Order.Status.REFUNDED in Order.TRANSITIONS[Order.Status.PAID]
    assert Order.TRANSITIONS[Order.Status.PARTIAL_REFUNDED] == {Order.Status.REFUNDED}
    assert Order.TRANSITIONS[Order.Status.REFUNDED] == set()


@pytest.mark.django_db
def test_reconciliation_reports_status_and_amount_discrepancies(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    closed_order = _make_paid_order(tenant, plan, owner, "rec-closed", "wechat")
    amount_order = _make_paid_order(tenant, plan, owner, "rec-amount", "alipay")
    stubs = {
        "wechat": StubProvider("wechat", status="closed", amount_cents=9900),
        "alipay": StubProvider("alipay", status="paid", amount_cents=8800),
    }
    _install_providers(monkeypatch, stubs)

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    by_order = {item["order_id"]: item for item in response.data["discrepancies"]}
    assert by_order[str(closed_order.id)]["kind"] == "status_mismatch"
    assert by_order[str(closed_order.id)]["local_status"] == Order.Status.PAID
    assert by_order[str(closed_order.id)]["provider_status"] == "closed"
    assert by_order[str(amount_order.id)]["kind"] == "amount_mismatch"
    assert by_order[str(amount_order.id)]["local_amount_cents"] == 9900
    assert by_order[str(amount_order.id)]["provider_amount_cents"] == 8800


@pytest.mark.django_db
def test_reconciliation_clean_window_reports_zero_discrepancies(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    _make_paid_order(tenant, plan, owner, "rec-clean")
    stub = StubProvider("wechat", status="paid", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    assert response.data["discrepancies"] == []
    assert response.data["unavailable_channels"] == []
    assert response.data["order_count"] == 1


@pytest.mark.django_db
def test_reconciliation_is_read_only(refund_setup, monkeypatch):
    owner, tenant, plan, budget = refund_setup
    order = _make_paid_order(tenant, plan, owner, "rec-readonly")
    stub = StubProvider("wechat", status="paid", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    status_before = order.status
    budget.refresh_from_db()
    budget_before = budget.monthly_limit
    grant_count = EntitlementGrant.objects.filter(order=order).count()

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    order.refresh_from_db()
    budget.refresh_from_db()
    assert order.status == status_before
    assert budget.monthly_limit == budget_before
    assert EntitlementGrant.objects.filter(order=order).count() == grant_count
    assert EntitlementReclaim.objects.filter(order=order).count() == 0


@pytest.mark.django_db
def test_reconciliation_rejects_missing_or_malformed_range(refund_setup, monkeypatch):
    owner, tenant, _plan, _ = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})
    client = APIClient()
    client.force_authenticate(user=owner)

    missing = client.get(_reconciliation_url(tenant.id))
    assert missing.status_code == 400
    assert missing.data["error"]["code"] == "RECONCILIATION_RANGE_INVALID"

    bad = client.get(_reconciliation_url(tenant.id, "not-a-date", "2026-11-23"))
    assert bad.status_code == 400
    assert bad.data["error"]["code"] == "RECONCILIATION_RANGE_INVALID"


@pytest.mark.django_db
def test_reclaim_ledger_is_append_only(refund_setup):
    owner, tenant, plan, budget = refund_setup
    order = _make_paid_order(tenant, plan, owner, "ledger-reclaim-1")
    grant = EntitlementGrant.objects.get(order=order)

    reclaim = EntitlementReclaim.objects.create(
        tenant=tenant,
        grant=grant,
        order=order,
        refund_reference="reclaim-ledger-1",
        refund_cents=3300,
        tokens=16_666,
        monthly_limit_before=budget.monthly_limit,
        monthly_limit_after=budget.monthly_limit - 16_666,
        reason="test ledger",
    )

    reclaim.tokens = 1
    with pytest.raises(ValueError, match="append-only"):
        reclaim.save()

    with pytest.raises(ValueError, match="cannot be deleted"):
        reclaim.delete()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EntitlementReclaim.objects.create(
                tenant=tenant,
                grant=grant,
                order=order,
                refund_reference="reclaim-ledger-1",
                refund_cents=100,
                tokens=1,
                monthly_limit_before=1,
                monthly_limit_after=0,
                reason="duplicate reference",
            )


# --------------------------------------------------------------------------- #
# Order close: provider-authoritative, idempotent and money-safe
# --------------------------------------------------------------------------- #


def _make_pending_order(tenant, plan, owner, key, channel="wechat"):
    return Order.objects.create(
        tenant=tenant,
        plan=plan,
        channel=channel,
        idempotency_key=key,
        created_by=owner,
        status=Order.Status.PENDING,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        provider_trade_no=f"fake-{key}",
    )


def _close_url(tenant_id, order_id):
    return f"/api/v1/tenants/{tenant_id}/billing/orders/{order_id}/close/"


@pytest.mark.django_db
def test_close_settles_the_trade_upstream_before_marking_the_order_closed(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat", status="pending")
    _install_providers(monkeypatch, {"wechat": stub})

    order = _make_pending_order(tenant, plan, owner, "close-happy")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_close_url(tenant.id, order.id))

    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.CLOSED
    assert stub.queries == 1
    assert stub.closed == [str(order.id)]


@pytest.mark.django_db
def test_close_refuses_an_order_the_provider_says_is_already_paid(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat", status="paid")
    _install_providers(monkeypatch, {"wechat": stub})

    order = _make_pending_order(tenant, plan, owner, "close-paid")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_close_url(tenant.id, order.id))

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ORDER_ALREADY_PAID"
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert stub.closed == []


@pytest.mark.django_db
def test_close_keeps_the_order_open_when_the_provider_query_fails(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat", status="pending")
    stub.fail_query = True
    _install_providers(monkeypatch, {"wechat": stub})

    order = _make_pending_order(tenant, plan, owner, "close-query-fail")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_close_url(tenant.id, order.id))

    assert response.status_code == 502
    assert response.data["error"]["code"] == "PAYMENT_QUERY_FAILED"
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert stub.closed == []


@pytest.mark.django_db
def test_close_keeps_the_order_open_when_the_provider_close_fails(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat", status="pending")
    stub.fail_close = True
    _install_providers(monkeypatch, {"wechat": stub})

    order = _make_pending_order(tenant, plan, owner, "close-fail")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_close_url(tenant.id, order.id))

    assert response.status_code == 502
    assert response.data["error"]["code"] == "PAYMENT_PROVIDER_UPSTREAM_ERROR"
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert stub.closed == []


@pytest.mark.django_db
def test_two_partial_refunds_carry_distinct_provider_references(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    stub = StubProvider("wechat")
    _install_providers(monkeypatch, {"wechat": stub})

    order = _make_paid_order(tenant, plan, owner, "refund-slices")
    client = APIClient()
    client.force_authenticate(user=owner)

    first = client.post(
        _refund_url(tenant.id, order.id), {"amount_cents": 4000, "reason": "partial one"}
    )
    assert first.status_code == 200
    second = client.post(
        _refund_url(tenant.id, order.id), {"amount_cents": 4000, "reason": "partial two"}
    )
    assert second.status_code == 200

    references = [reference for _, _, reference in stub.refunds]
    assert len(references) == 2
    assert references[0] != references[1]
    assert all(reference.startswith(f"refund-{order.id}-") for reference in references)
    order.refresh_from_db()
    assert order.status == Order.Status.PARTIAL_REFUNDED
    assert order.refunded_cents == 8000


@pytest.mark.django_db
def test_reconciliation_confirms_every_refund_slice_with_the_provider(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    order = _make_paid_order(tenant, plan, owner, "rec-refund-ok")
    stub = StubProvider("wechat", status="partial_refunded", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    EntitlementReclaim.objects.create(
        tenant=tenant,
        order=order,
        refund_reference=f"refund-{order.id}-abc123",
        refund_cents=4900,
        reason="goodwill",
    )
    order.refunded_cents = 4900
    order.status = Order.Status.PARTIAL_REFUNDED
    order.save(update_fields=["refunded_cents", "status", "updated_at"])
    stub.refund_query_cents = 4900

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    assert response.data["refund_slice_count"] == 1
    assert response.data["refund_discrepancies"] == []
    assert stub.refund_queries == [f"refund-{order.id}-abc123"]


@pytest.mark.django_db
def test_reconciliation_flags_a_refund_the_provider_has_not_settled(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    order = _make_paid_order(tenant, plan, owner, "rec-refund-pending")
    stub = StubProvider("wechat", status="partial_refunded", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    EntitlementReclaim.objects.create(
        tenant=tenant,
        order=order,
        refund_reference=f"refund-{order.id}-def456",
        refund_cents=4900,
    )
    order.refunded_cents = 4900
    order.status = Order.Status.PARTIAL_REFUNDED
    order.save(update_fields=["refunded_cents", "status", "updated_at"])
    stub.refund_query_status = "unknown"

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    item = response.data["refund_discrepancies"][0]
    assert item["kind"] == "refund_status_mismatch"
    assert item["local_refund_cents"] == 4900
    assert item["provider_status"] == "unknown"
    assert item["refund_reference"] == f"refund-{order.id}-def456"


@pytest.mark.django_db
def test_reconciliation_flags_a_refund_amount_the_provider_disagrees_with(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    order = _make_paid_order(tenant, plan, owner, "rec-refund-amount")
    stub = StubProvider("wechat", status="refunded", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    EntitlementReclaim.objects.create(
        tenant=tenant,
        order=order,
        refund_reference=f"refund-{order.id}-ghi789",
        refund_cents=9900,
    )
    order.refunded_cents = 9900
    order.status = Order.Status.REFUNDED
    order.save(update_fields=["refunded_cents", "status", "updated_at"])
    stub.refund_query_cents = 8900

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    item = response.data["refund_discrepancies"][0]
    assert item["kind"] == "refund_amount_mismatch"
    assert item["provider_refund_cents"] == 8900


@pytest.mark.django_db
def test_reconciliation_marks_the_channel_unavailable_when_a_refund_query_fails(
    refund_setup, monkeypatch
):
    owner, tenant, plan, _ = refund_setup
    order = _make_paid_order(tenant, plan, owner, "rec-refund-down")
    stub = StubProvider("wechat", status="paid", amount_cents=9900)
    _install_providers(monkeypatch, {"wechat": stub})

    EntitlementReclaim.objects.create(
        tenant=tenant,
        order=order,
        refund_reference=f"refund-{order.id}-jkl012",
        refund_cents=1000,
    )
    order.refunded_cents = 1000
    order.status = Order.Status.PARTIAL_REFUNDED
    order.save(update_fields=["refunded_cents", "status", "updated_at"])
    stub.fail_refund_query = True

    client = APIClient()
    client.force_authenticate(user=owner)
    start, end = _today_window()
    response = client.get(_reconciliation_url(tenant.id, start, end))

    assert response.status_code == 200
    assert response.data["refund_discrepancies"] == []
    assert response.data["unavailable_channels"] == ["wechat"]
