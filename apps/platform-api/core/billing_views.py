"""Billing HTTP endpoints: plans, subscription, usage, orders and callbacks.

Authorization follows the contract: every tenant route enforces membership,
order creation requires tenant admin, callbacks are unauthenticated but
fail-closed signature verified. Paid state can only be produced by a verified
callback from a configured provider.
"""

from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import subscription_service
from core.api_errors import ErrorEnvelopeMixin
from core.audit import record_audit_event
from core.billing_models import (
    EntitlementGrant,
    EntitlementReclaim,
    Order,
    PaymentTransactionAudit,
    Plan,
    Subscription,
)
from core.models import AuditEvent, ModelProvider, Tenant, TenantTokenBudget
from core.payments import registry
from core.payments.base import PaymentProviderError
from core.permissions import IsTenantAdmin
from core.refund_service import reclaim_for_refund
from core.subscription_service import SubscriptionStateError
from core.token_budget import (
    get_tenant_token_usage,
    get_tenant_usage_history,
    local_day_bounds,
    local_month_bounds,
)

SUPPORTED_CHANNELS = ("wechat", "alipay")


class RegionDenied(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _error(code: str, message: str, details: dict | None = None, http_status: int = 400):
    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=http_status,
    )


# Metadata keys that must never reach the payment ledger, even by accident.
_LEDGER_SENSITIVE_PARTS = (
    "private",
    "secret",
    "key",
    "cert",
    "pem",
    "token",
    "authorization",
    "credential",
    "signature",
)


def _sanitize_ledger_metadata(metadata: dict | None) -> dict:
    """Drop any key that could carry key material or a signing artifact."""

    safe: dict = {}
    for key, value in (metadata or {}).items():
        normalized = str(key).lower().replace("-", "_")
        if any(part in normalized for part in _LEDGER_SENSITIVE_PARTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)[:80]] = value
    return safe


def _record_payment_audit(
    *,
    tenant,
    order,
    channel: str,
    direction: str,
    event_type: str,
    status: str = "",
    amount_cents: int = 0,
    provider_trade_no: str = "",
    error_code: str = "",
    metadata: dict | None = None,
) -> None:
    """Append a payment request/callback ledger row.

    Only non-secret fields are stored; the writer refuses any metadata key
    that looks like key material, a certificate or a signature.
    """

    PaymentTransactionAudit.objects.create(
        tenant=tenant,
        order=order,
        channel=channel,
        direction=direction,
        event_type=event_type,
        status=status,
        amount_cents=amount_cents,
        provider_trade_no=provider_trade_no,
        error_code=error_code,
        metadata=_sanitize_ledger_metadata(metadata),
    )


def _check_region_gate(tenant: Tenant, plan: Plan) -> None:
    """Fail-closed region gate driven by ``ModelProvider.deployment_region``.

    ``""`` (unclassified) and any non-``domestic`` value are not purchasable.
    No vendor list is hardcoded; the decision reads the field.
    """

    if plan.region_scope != Plan.RegionScope.DOMESTIC:
        raise RegionDenied("PURCHASE_REGION_NOT_SUPPORTED")
    providers = ModelProvider.objects.filter(tenant=tenant, enabled=True)
    for provider in providers:
        region = (provider.deployment_region or "").strip()
        if region != "domestic":
            raise RegionDenied("PURCHASE_REGION_NOT_SUPPORTED")


def _plan_dict(plan: Plan, purchasable: bool, reason_code: str | None) -> dict:
    return {
        "id": str(plan.id),
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "interval": plan.interval,
        "price_cents": plan.price_cents,
        "currency": plan.currency,
        "token_quota_daily": plan.token_quota_daily,
        "token_quota_monthly": plan.token_quota_monthly,
        "features": plan.features,
        "region_scope": plan.region_scope,
        "active": plan.active,
        "purchasable": purchasable,
        "reason_code": reason_code,
    }


def _subscription_dict(subscription: Subscription | None) -> dict | None:
    if subscription is None:
        return None
    return {
        "id": str(subscription.id),
        "plan": subscription.plan.code,
        "status": subscription.status,
        "started_at": subscription.started_at.isoformat()
        if subscription.started_at
        else None,
        "current_period_start": subscription.current_period_start.isoformat()
        if subscription.current_period_start
        else None,
        "current_period_end": subscription.current_period_end.isoformat()
        if subscription.current_period_end
        else None,
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "renewal_mode": subscription.renewal_mode,
        "cancel_effective_at": subscription.current_period_end.isoformat()
        if subscription.cancel_at_period_end and subscription.current_period_end
        else None,
    }


def _order_dict(order: Order, payment: dict | None = None) -> dict:
    payload = {
        "id": str(order.id),
        "tenant": str(order.tenant_id),
        "plan": order.plan.code,
        "status": order.status,
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "channel": order.channel,
        "provider_trade_no": order.provider_trade_no,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "idempotency_key": order.idempotency_key,
        "created_at": order.created_at.isoformat(),
    }
    if payment is not None:
        payload["payment"] = payment
    return payload


class TenantScopedView(ErrorEnvelopeMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get_tenant(self) -> Tenant:
        return get_object_or_404(
            Tenant.objects.filter(memberships__user=self.request.user),
            pk=self.kwargs["tenant_id"],
        )


class BillingCapabilitiesView(TenantScopedView):
    def get(self, request, tenant_id):
        self.get_tenant()
        providers = registry.build_providers()
        available = bool(providers)
        channels = [channel for channel in sorted(providers) if channel in SUPPORTED_CHANNELS]
        reason_code = None if available else "PAYMENT_PROVIDER_NOT_CONFIGURED"
        return Response(
            {"payment": {"available": available, "channels": channels, "reason_code": reason_code}}
        )


class PlansView(TenantScopedView):
    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        providers = registry.build_providers()
        results = []
        for plan in Plan.objects.filter(active=True):
            purchasable = True
            reason_code = None
            if not providers:
                purchasable = False
                reason_code = "PAYMENT_PROVIDER_NOT_CONFIGURED"
            else:
                try:
                    _check_region_gate(tenant, plan)
                except RegionDenied:
                    purchasable = False
                    reason_code = "PURCHASE_REGION_NOT_SUPPORTED"
            results.append(_plan_dict(plan, purchasable, reason_code))
        return Response({"results": results})


class SubscriptionView(TenantScopedView):
    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        subscription = (
            Subscription.objects.filter(tenant=tenant).order_by("-created_at").first()
        )
        return Response(
            {"subscription": _subscription_dict(subscription), "source": "authoritative"}
        )


class _SubscriptionAdminView(TenantScopedView):
    def get_permissions(self):
        return [IsAuthenticated(), IsTenantAdmin()]


def _subscription_error(exc: SubscriptionStateError):
    return _error(
        exc.code,
        exc.message,
        details=exc.details,
        http_status=exc.http_status,
    )


class SubscriptionTrialView(_SubscriptionAdminView):
    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        plan_code = str(request.data.get("plan_code", "")).strip()
        if not plan_code:
            return _error(
                "SUBSCRIPTION_PLAN_REQUIRED",
                "plan_code is required.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        plan = get_object_or_404(Plan, code=plan_code, active=True)
        try:
            subscription = subscription_service.start_trial(
                tenant=tenant, plan=plan, request=request
            )
        except SubscriptionStateError as exc:
            return _subscription_error(exc)
        return Response(
            {"subscription": _subscription_dict(subscription)},
            status=status.HTTP_201_CREATED,
        )


class SubscriptionCancelView(_SubscriptionAdminView):
    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        subscription = get_object_or_404(Subscription.objects.filter(tenant=tenant))
        mode = str(request.data.get("mode", "period_end")).strip() or "period_end"
        try:
            subscription = subscription_service.cancel_subscription(
                subscription=subscription, mode=mode, request=request
            )
        except SubscriptionStateError as exc:
            return _subscription_error(exc)
        return Response({"subscription": _subscription_dict(subscription)})


class SubscriptionRenewView(_SubscriptionAdminView):
    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        subscription = get_object_or_404(Subscription.objects.filter(tenant=tenant))
        order_id = str(request.data.get("order_id", "")).strip()
        if not order_id:
            return _error(
                "SUBSCRIPTION_RENEWAL_ORDER_REQUIRED",
                "order_id is required.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        order = get_object_or_404(Order.objects.filter(tenant=tenant), pk=order_id)
        try:
            subscription, grant = subscription_service.renew_subscription(
                subscription=subscription, order=order, request=request
            )
        except SubscriptionStateError as exc:
            return _subscription_error(exc)
        return Response(
            {
                "subscription": _subscription_dict(subscription),
                "grant": {"id": str(grant.id), "tokens": grant.tokens},
            }
        )


class SubscriptionSettleView(_SubscriptionAdminView):
    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        settled = subscription_service.settle_due_subscriptions(
            request=request, tenant=tenant
        )
        return Response(
            {
                "count": len(settled),
                "settled": [_subscription_dict(item) for item in settled],
            }
        )


class UsageView(TenantScopedView):
    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        budget, _ = TenantTokenBudget.objects.get_or_create(tenant=tenant)
        usage = get_tenant_token_usage(tenant.id)
        day_start, day_end = local_day_bounds()
        _, month_end = local_month_bounds()
        daily_used = int(usage["daily_used"])
        monthly_used = int(usage["monthly_used"])
        daily_limit = int(budget.daily_limit)
        monthly_limit = int(budget.monthly_limit)
        grants = EntitlementGrant.objects.filter(tenant=tenant).order_by("-created_at")
        grant_history = [
            {
                "id": str(grant.id),
                "order": str(grant.order_id),
                "period": grant.period,
                "tokens": grant.tokens,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                "reason": grant.reason,
                "created_at": grant.created_at.isoformat(),
            }
            for grant in grants
        ]
        return Response(
            {
                "daily_limit": daily_limit,
                "monthly_limit": monthly_limit,
                **usage,
                "remaining_daily": max(0, daily_limit - daily_used),
                "remaining_monthly": max(0, monthly_limit - monthly_used),
                "reset_at": day_end.isoformat(),
                "monthly_reset_at": month_end.isoformat(),
                "period_start": day_start.isoformat(),
                "period_end": day_end.isoformat(),
                "limits_source": "tenant_budget",
                "history": get_tenant_usage_history(tenant.id),
                "grants": grant_history,
            }
        )


class OrderListCreateView(TenantScopedView):
    def get_permissions(self):
        permissions = [IsAuthenticated]
        if self.request.method == "POST":
            permissions.append(IsTenantAdmin)
        return [permission() for permission in permissions]

    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        orders = Order.objects.filter(tenant=tenant).select_related("plan")
        return Response({"results": [_order_dict(order) for order in orders]})

    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        plan_code = str(request.data.get("plan_code", "")).strip()
        channel = str(request.data.get("channel", "")).strip()
        idempotency_key = str(request.data.get("idempotency_key", "")).strip()

        if channel not in SUPPORTED_CHANNELS:
            return _error(
                "PAYMENT_CHANNEL_NOT_SUPPORTED",
                f"Channel {channel!r} is not supported.",
                details={"channel": [channel]},
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        if not plan_code or not idempotency_key:
            return _error(
                "ORDER_INVALID",
                "plan_code and idempotency_key are required.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )

        plan = get_object_or_404(Plan, code=plan_code, active=True)

        try:
            _check_region_gate(tenant, plan)
        except RegionDenied as exc:
            record_audit_event(
                request,
                tenant,
                "billing.purchase.region_denied",
                "order",
                metadata={"reason_code": exc.reason_code, "plan_code": plan.code},
                outcome=AuditEvent.Outcome.DENIED,
            )
            return _error(
                exc.reason_code,
                "The selected plan is not purchasable in this region.",
                http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        providers = registry.build_providers()
        provider = providers.get(channel)
        if provider is None:
            record_audit_event(
                request,
                tenant,
                "billing.purchase.provider_unavailable",
                "order",
                metadata={"channel": channel, "plan_code": plan.code},
                outcome=AuditEvent.Outcome.DENIED,
            )
            return _error(
                "PAYMENT_PROVIDER_NOT_CONFIGURED",
                "No payment provider is configured for this channel.",
                http_status=status.HTTP_409_CONFLICT,
            )

        existing = Order.objects.filter(
            tenant=tenant, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return Response(_order_dict(existing), status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                order = Order.objects.create(
                    tenant=tenant,
                    plan=plan,
                    status=Order.Status.CREATED,
                    amount_cents=plan.price_cents,
                    currency=plan.currency,
                    channel=channel,
                    idempotency_key=idempotency_key,
                    created_by=request.user,
                )
                instructions = provider.create_payment(order)
                order.provider_trade_no = instructions.get("trade_no", "")
                order.status = Order.Status.PENDING
                order.save(update_fields=["provider_trade_no", "status", "updated_at"])
        except PaymentProviderError as exc:
            record_audit_event(
                request,
                tenant,
                "billing.purchase.provider_error",
                "order",
                outcome=AuditEvent.Outcome.DENIED,
                metadata={"channel": channel, "reason_code": exc.code},
            )
            _record_payment_audit(
                tenant=tenant,
                order=None,
                channel=channel,
                direction="request",
                event_type="payment.request.error",
                error_code=exc.code,
                metadata={"plan_code": plan.code},
            )
            return _error(
                exc.code,
                "The payment provider could not process the request.",
                details={"channel": channel},
                http_status=status.HTTP_502_BAD_GATEWAY,
            )

        record_audit_event(
            request,
            tenant,
            "billing.order.created",
            "order",
            order.id,
            metadata={"channel": channel, "plan_code": plan.code},
        )
        _record_payment_audit(
            tenant=tenant,
            order=order,
            channel=channel,
            direction="request",
            event_type="payment.request.created",
            provider_trade_no=order.provider_trade_no,
            amount_cents=order.amount_cents,
            metadata={"plan_code": plan.code},
        )
        return Response(
            _order_dict(order, payment=instructions),
            status=status.HTTP_201_CREATED,
        )


class OrderDetailView(TenantScopedView):
    def get(self, request, tenant_id, order_id):
        tenant = self.get_tenant()
        order = get_object_or_404(
            Order.objects.filter(tenant=tenant).select_related("plan"),
            pk=order_id,
        )
        payment = None
        if order.status == Order.Status.PENDING:
            provider = registry.get_provider(order.channel)
            if provider is not None:
                payment = provider.create_payment(order)
        return Response(_order_dict(order, payment=payment))


class OrderCloseView(TenantScopedView):
    def get_permissions(self):
        return [IsAuthenticated(), IsTenantAdmin()]

    def post(self, request, tenant_id, order_id):
        tenant = self.get_tenant()
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().filter(tenant=tenant),
                pk=order_id,
            )
            if order.status not in {Order.Status.CREATED, Order.Status.PENDING}:
                return _error(
                    "ORDER_STATE_CONFLICT",
                    f"Order in status {order.status} cannot be closed.",
                    http_status=status.HTTP_409_CONFLICT,
                )
            order.transition(Order.Status.CLOSED)
        record_audit_event(
            request, tenant, "billing.order.closed", "order", order.id
        )
        return Response(_order_dict(order))


def _parse_refund_amount(raw) -> int | None:
    """Parse the optional refund slice; ``None`` means refund everything left."""

    if raw is None:
        return None
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("amount_cents must be an integer") from exc
    if amount <= 0:
        raise ValueError("amount_cents must be positive")
    return amount


class OrderRefundView(TenantScopedView):
    def get_permissions(self):
        return [IsAuthenticated(), IsTenantAdmin()]

    def post(self, request, tenant_id, order_id):
        tenant = self.get_tenant()
        reason = str(request.data.get("reason", "")).strip() or "refund"

        try:
            requested = _parse_refund_amount(request.data.get("amount_cents"))
        except ValueError as exc:
            return _error(
                "REFUND_AMOUNT_INVALID",
                str(exc),
                details={"amount_cents": [str(request.data.get("amount_cents"))]},
                http_status=status.HTTP_400_BAD_REQUEST,
            )

        order = None
        receipt: dict = {}
        refund_reference = ""
        refund_cents = 0
        try:
            with transaction.atomic():
                order = get_object_or_404(
                    Order.objects.select_for_update().filter(tenant=tenant),
                    pk=order_id,
                )
                if order.status not in {
                    Order.Status.PAID,
                    Order.Status.PARTIAL_REFUNDED,
                }:
                    return _error(
                        "ORDER_STATE_CONFLICT",
                        f"Order in status {order.status} cannot be refunded.",
                        http_status=status.HTTP_409_CONFLICT,
                    )
                refundable = order.amount_cents - order.refunded_cents
                refund_cents = requested if requested is not None else refundable
                if refund_cents > refundable:
                    return _error(
                        "REFUND_AMOUNT_EXCEEDED",
                        "Refund amount exceeds the remaining refundable balance.",
                        details={"refundable_cents": refundable},
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                provider = registry.get_provider(order.channel)
                receipt = (
                    provider.refund(order, reason, amount_cents=refund_cents)
                    if provider
                    else {}
                )
                refund_reference = (
                    receipt.get("refund_id")
                    or receipt.get("out_refund_no")
                    or f"local-refund-{order.id}-{order.refunded_cents}-{refund_cents}"
                )
                # Reclaim runs on the shared idempotent ledger path; a replay of
                # this refund reference reuses the existing row.
                reclaim_for_refund(
                    order=order,
                    refund_reference=refund_reference,
                    refund_cents=refund_cents,
                    reason=reason,
                )
                order.refunded_cents += refund_cents
                target = (
                    Order.Status.REFUNDED
                    if order.refunded_cents >= order.amount_cents
                    else Order.Status.PARTIAL_REFUNDED
                )
                order.transition(target)
                order.save(update_fields=["refunded_cents", "status", "updated_at"])
        except PaymentProviderError as exc:
            record_audit_event(
                request,
                tenant,
                "billing.refund.provider_error",
                "order",
                order.id if order else None,
                outcome=AuditEvent.Outcome.DENIED,
                metadata={"channel": order.channel if order else "", "reason_code": exc.code},
            )
            _record_payment_audit(
                tenant=tenant,
                order=order,
                channel=order.channel if order else "",
                direction="request",
                event_type="payment.request.refund_error",
                amount_cents=refund_cents,
                error_code=exc.code,
                metadata={"reason": reason},
            )
            return _error(
                exc.code,
                "The payment provider could not process the refund.",
                details={"channel": order.channel if order else ""},
                http_status=status.HTTP_502_BAD_GATEWAY,
            )
        record_audit_event(
            request,
            tenant,
            "billing.order.refunded",
            "order",
            order.id,
            metadata={"reason": reason, "refund_cents": refund_cents},
        )
        _record_payment_audit(
            tenant=tenant,
            order=order,
            channel=order.channel,
            direction="request",
            event_type="payment.request.refund",
            amount_cents=refund_cents,
            provider_trade_no=order.provider_trade_no,
            metadata={"reason": reason, "refund_reference": refund_reference},
        )
        return Response(_order_dict(order, payment=receipt))


def _parse_local_day_range(start_raw: str, end_raw: str) -> tuple:
    """Parse ``YYYY-MM-DD`` bounds on the local day boundary (inclusive both).

    Reuses the local-timezone day convention from :func:`local_day_bounds`;
    ``end`` is the first instant after the end day, so the filter is
    ``created_at__gte=start, created_at__lt=end``.
    """

    if not start_raw or not end_raw:
        raise ValueError("start and end are required")
    try:
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except ValueError as exc:
        raise ValueError("dates must use YYYY-MM-DD format") from exc
    if end_date < start_date:
        raise ValueError("end date precedes start date")
    local_now = timezone.localtime()
    start = local_now.replace(
        year=start_date.year,
        month=start_date.month,
        day=start_date.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = local_now.replace(
        year=end_date.year,
        month=end_date.month,
        day=end_date.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=1)
    return start, end


_RECONCILED_STATUSES = {
    Order.Status.PAID,
    Order.Status.PARTIAL_REFUNDED,
    Order.Status.REFUNDED,
}

_PROVIDER_MISSING_STATES = {"", "unknown", "not_found", "not_exist"}


class ReconciliationView(_SubscriptionAdminView):
    """Read-only date-window reconciliation report for one tenant.

    The endpoint never writes business data and never changes an order status.
    A channel without a configured provider is listed under
    ``unavailable_channels`` instead of failing the whole report.
    """

    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        start_raw = str(request.query_params.get("start", "")).strip()
        end_raw = str(request.query_params.get("end", "")).strip()
        try:
            start_dt, end_dt = _parse_local_day_range(start_raw, end_raw)
        except ValueError as exc:
            return _error(
                "RECONCILIATION_RANGE_INVALID",
                str(exc),
                details={"start": [start_raw], "end": [end_raw]},
                http_status=status.HTTP_400_BAD_REQUEST,
            )

        orders = (
            Order.objects.filter(
                tenant=tenant,
                created_at__gte=start_dt,
                created_at__lt=end_dt,
            )
            .select_related("plan")
            .order_by("created_at")
        )

        status_counts: dict[str, int] = {}
        channel_amounts: dict[str, dict] = {}
        for order in orders:
            status_counts[order.status] = status_counts.get(order.status, 0) + 1
            aggregate = channel_amounts.setdefault(
                order.channel,
                {"order_count": 0, "amount_cents": 0, "refunded_cents": 0},
            )
            aggregate["order_count"] += 1
            aggregate["amount_cents"] += order.amount_cents
            aggregate["refunded_cents"] += order.refunded_cents

        providers = registry.build_providers()
        unavailable = {
            order.channel for order in orders if order.channel not in providers
        }

        discrepancies: list[dict] = []
        for order in orders:
            if order.status not in _RECONCILED_STATUSES:
                continue
            provider = providers.get(order.channel)
            if provider is None:
                continue
            try:
                state = provider.query_order(order)
            except PaymentProviderError:
                unavailable.add(order.channel)
                continue
            item = self._compare_order(order, state)
            if item is not None:
                discrepancies.append(item)

        unavailable_channels = sorted(unavailable)

        return Response(
            {
                "start": start_raw,
                "end": end_raw,
                "order_count": len(orders),
                "status_counts": status_counts,
                "channel_amounts": channel_amounts,
                "unavailable_channels": unavailable_channels,
                "discrepancies": discrepancies,
            }
        )

    @staticmethod
    def _compare_order(order: Order, state) -> dict | None:
        provider_status = (getattr(state, "status", "") or "unknown").strip()
        provider_amount = getattr(state, "amount_cents", None)
        base = {
            "order_id": str(order.id),
            "channel": order.channel,
            "local_status": order.status,
            "provider_status": provider_status,
            "local_amount_cents": order.amount_cents,
            "provider_amount_cents": provider_amount,
        }
        if provider_status in _PROVIDER_MISSING_STATES:
            return {**base, "kind": "provider_not_found"}
        if provider_status != order.status:
            return {**base, "kind": "status_mismatch"}
        if provider_amount is not None and provider_amount != order.amount_cents:
            return {**base, "kind": "amount_mismatch"}
        return None


@transaction.atomic
def _credit_entitlement(order: Order) -> EntitlementGrant:
    """Create the grant row and move the budget through that ledger record.

    The budget limit is never written directly outside this ledger path, and a
    single order credits its quota exactly once.
    """

    # Serialize re-entry on the order row: a replay and a concurrent credit
    # path both take this lock, so only one grant row can ever be inserted.
    order = Order.objects.select_for_update().get(pk=order.pk)
    existing = EntitlementGrant.objects.filter(order=order).first()
    if existing is not None:
        return existing

    subscription, _ = Subscription.objects.get_or_create(
        tenant=order.tenant,
        plan=order.plan,
        defaults={
            "status": Subscription.Status.ACTIVE,
            "started_at": timezone.now(),
            "current_period_start": timezone.now(),
            "current_period_end": timezone.now() + timedelta(days=30),
        },
    )
    grant = EntitlementGrant(
        tenant=order.tenant,
        subscription=subscription,
        order=order,
        period=EntitlementGrant.Period.MONTHLY,
        tokens=order.plan.token_quota_monthly,
        expires_at=timezone.now() + timedelta(days=30),
        reason=f"plan:{order.plan.code}",
    )
    try:
        with transaction.atomic():
            grant.save()
    except IntegrityError:
        # Another writer won the race; its row is the authoritative grant.
        return EntitlementGrant.objects.get(order=order)

    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(
        tenant=order.tenant
    )
    budget.monthly_limit += grant.tokens
    budget.save(update_fields=["monthly_limit", "updated_at"])
    return grant


class PaymentCallbackView(ErrorEnvelopeMixin, APIView):
    permission_classes = [AllowAny]

    def post(self, request, tenant_id, channel):
        if channel not in SUPPORTED_CHANNELS:
            return _error(
                "PAYMENT_CHANNEL_NOT_SUPPORTED",
                f"Channel {channel!r} is not supported.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        provider = registry.get_provider(channel)
        if provider is None:
            return _error(
                "PAYMENT_PROVIDER_NOT_CONFIGURED",
                "No payment provider is configured for this channel.",
                http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        result = provider.verify_callback(request.headers, request.body)
        if not result.ok:
            record_audit_event(
                request,
                None,
                "billing.callback.signature_invalid",
                "payment_callback",
                resource_id=channel,
                outcome=AuditEvent.Outcome.DENIED,
                metadata={"channel": channel, "message": result.message},
            )
            _record_payment_audit(
                tenant=None,
                order=None,
                channel=channel,
                direction="callback",
                event_type="payment.callback.rejected",
                error_code="PAYMENT_SIGNATURE_INVALID",
                metadata={"message": result.message},
            )
            return _error(
                "PAYMENT_SIGNATURE_INVALID",
                "Callback signature verification failed.",
                details={"message": result.message},
                http_status=status.HTTP_400_BAD_REQUEST,
            )

        order = get_object_or_404(
            Order,
            channel=channel,
            provider_trade_no=result.order_reference or "",
        )

        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            # Refund notifications carry the refund slice, not the order total,
            # so the payment amount-mismatch guard does not apply to them.
            is_refund_notify = result.status in {
                "refunded",
                "refund",
                "refund_abnormal",
            }
            mismatch = (
                not is_refund_notify
                and result.amount_cents is not None
                and result.amount_cents != locked.amount_cents
            )
            if mismatch:
                record_audit_event(
                    None,
                    locked.tenant,
                    "billing.callback.amount_mismatch",
                    "payment_callback",
                    locked.id,
                    metadata={
                        "channel": locked.channel,
                        "expected_cents": locked.amount_cents,
                        "actual_cents": result.amount_cents,
                    },
                    outcome=AuditEvent.Outcome.DENIED,
                )
                _record_payment_audit(
                    tenant=locked.tenant,
                    order=locked,
                    channel=locked.channel,
                    direction="callback",
                    event_type="payment.callback.amount_mismatch",
                    amount_cents=locked.amount_cents,
                    provider_trade_no=locked.provider_trade_no,
                    error_code="PAYMENT_AMOUNT_MISMATCH",
                    metadata={
                        "expected_cents": locked.amount_cents,
                        "actual_cents": result.amount_cents,
                    },
                )
                return _error(
                    "PAYMENT_AMOUNT_MISMATCH",
                    "Callback amount does not match the order amount.",
                    details={
                        "expected_cents": locked.amount_cents,
                        "actual_cents": result.amount_cents,
                    },
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            applied = self._apply_callback(locked, result)

        return Response({"status": locked.status, "applied": applied})

    def _apply_callback(self, order: Order, result) -> bool:
        if result.status in {"refunded", "refund", "refund_abnormal"}:
            return self._apply_refund_callback(order, result)

        target = {
            "paid": Order.Status.PAID,
            "failed": Order.Status.FAILED,
            "closed": Order.Status.CLOSED,
        }.get(result.status)

        if target is None:
            record_audit_event(
                None,
                order.tenant,
                "billing.callback.unknown_status",
                "payment_callback",
                order.id,
                metadata={"channel": order.channel, "status": result.status},
                outcome=AuditEvent.Outcome.FAILURE,
            )
            _record_payment_audit(
                tenant=order.tenant,
                order=order,
                channel=order.channel,
                direction="callback",
                event_type="payment.callback.unknown_status",
                status=result.status,
                provider_trade_no=order.provider_trade_no,
                error_code="PAYMENT_CALLBACK_STATUS_UNKNOWN",
            )
            return False

        if order.status == target:
            # Repeated callback: idempotent no-op.
            _record_payment_audit(
                tenant=order.tenant,
                order=order,
                channel=order.channel,
                direction="callback",
                event_type="payment.callback.duplicate",
                status=target,
                provider_trade_no=order.provider_trade_no,
            )
            return False

        if not order.can_transition(target):
            # Out-of-order callback: never move state backwards.
            record_audit_event(
                None,
                order.tenant,
                "billing.callback.out_of_order",
                "payment_callback",
                order.id,
                metadata={
                    "channel": order.channel,
                    "from_status": order.status,
                    "to_status": target,
                },
                outcome=AuditEvent.Outcome.DENIED,
            )
            _record_payment_audit(
                tenant=order.tenant,
                order=order,
                channel=order.channel,
                direction="callback",
                event_type="payment.callback.out_of_order",
                status=target,
                provider_trade_no=order.provider_trade_no,
                metadata={"from_status": order.status, "to_status": target},
                error_code="PAYMENT_CALLBACK_OUT_OF_ORDER",
            )
            return False

        order.transition(target)
        if target == Order.Status.PAID:
            _credit_entitlement(order)
        record_audit_event(
            None,
            order.tenant,
            f"billing.order.{target}",
            "payment_callback",
            order.id,
            metadata={"channel": order.channel},
        )
        _record_payment_audit(
            tenant=order.tenant,
            order=order,
            channel=order.channel,
            direction="callback",
            event_type="payment.callback.verified",
            status=target,
            amount_cents=order.amount_cents,
            provider_trade_no=order.provider_trade_no,
        )
        return True

    def _apply_refund_callback(self, order: Order, result) -> bool:
        """Apply a verified provider refund notification.

        Replays of the same refund reference reuse the existing reclaim row;
        the refund path never re-issues an entitlement grant. ABNORMAL/CLOSED
        refunds are recorded only: no state change, no quota movement.
        """

        if result.status == "refund_abnormal":
            record_audit_event(
                None,
                order.tenant,
                "billing.callback.refund_abnormal",
                "payment_callback",
                order.id,
                metadata={"channel": order.channel, "status": result.status},
                outcome=AuditEvent.Outcome.FAILURE,
            )
            _record_payment_audit(
                tenant=order.tenant,
                order=order,
                channel=order.channel,
                direction="callback",
                event_type="payment.callback.refund_abnormal",
                status=result.status,
                amount_cents=result.amount_cents or 0,
                provider_trade_no=order.provider_trade_no,
            )
            return False

        refund_reference = result.refund_reference or (
            f"callback-refund-{order.id}-{result.amount_cents or 0}"
        )
        already = EntitlementReclaim.objects.filter(
            refund_reference=refund_reference
        ).first()
        if already is None:
            reclaim = reclaim_for_refund(
                order=order,
                refund_reference=refund_reference,
                refund_cents=result.amount_cents or order.amount_cents,
                reason="refund_callback",
            )
            new_refunded_cents = order.refunded_cents + reclaim.refund_cents
            target = (
                Order.Status.REFUNDED
                if new_refunded_cents >= order.amount_cents
                else Order.Status.PARTIAL_REFUNDED
            )
            if order.can_transition(target):
                order.refunded_cents = new_refunded_cents
                order.transition(target)
                order.save(update_fields=["refunded_cents", "status", "updated_at"])

        record_audit_event(
            None,
            order.tenant,
            "billing.order.refund_callback",
            "payment_callback",
            order.id,
            metadata={"channel": order.channel, "refund_reference": refund_reference},
        )
        _record_payment_audit(
            tenant=order.tenant,
            order=order,
            channel=order.channel,
            direction="callback",
            event_type="payment.callback.refund",
            status=result.status,
            amount_cents=result.amount_cents or order.amount_cents,
            provider_trade_no=order.provider_trade_no,
            metadata={"refund_reference": refund_reference},
        )
        return True
