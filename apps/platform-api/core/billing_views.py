"""Billing HTTP endpoints: plans, subscription, usage, orders and callbacks.

Authorization follows the contract: every tenant route enforces membership,
order creation requires tenant admin, callbacks are unauthenticated but
fail-closed signature verified. Paid state can only be produced by a verified
callback from a configured provider.
"""

from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import subscription_service
from core.api_errors import ErrorEnvelopeMixin
from core.audit import record_audit_event
from core.billing_models import EntitlementGrant, Order, Plan, Subscription
from core.models import AuditEvent, ModelProvider, Tenant, TenantTokenBudget
from core.payments import registry
from core.permissions import IsTenantAdmin
from core.subscription_service import SubscriptionStateError
from core.token_budget import (
    get_tenant_token_usage,
    get_tenant_usage_history,
    local_day_bounds,
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
                "daily_limit": budget.daily_limit,
                "monthly_limit": budget.monthly_limit,
                **usage,
                "period_start": day_start.isoformat(),
                "period_end": day_end.isoformat(),
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

        record_audit_event(
            request,
            tenant,
            "billing.order.created",
            "order",
            order.id,
            metadata={"channel": channel, "plan_code": plan.code},
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


class OrderRefundView(TenantScopedView):
    def get_permissions(self):
        return [IsAuthenticated(), IsTenantAdmin()]

    def post(self, request, tenant_id, order_id):
        tenant = self.get_tenant()
        reason = str(request.data.get("reason", "")).strip() or "refund"
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().filter(tenant=tenant),
                pk=order_id,
            )
            if order.status != Order.Status.PAID:
                return _error(
                    "ORDER_STATE_CONFLICT",
                    f"Order in status {order.status} cannot be refunded.",
                    http_status=status.HTTP_409_CONFLICT,
                )
            provider = registry.get_provider(order.channel)
            receipt = provider.refund(order, reason) if provider else {}
            order.transition(Order.Status.REFUNDED)
        record_audit_event(
            request,
            tenant,
            "billing.order.refunded",
            "order",
            order.id,
            metadata={"reason": reason},
        )
        return Response(_order_dict(order, payment=receipt))


@transaction.atomic
def _credit_entitlement(order: Order) -> EntitlementGrant:
    """Create the grant row and move the budget through that ledger record.

    The budget limit is never written directly outside this ledger path, and a
    single order credits its quota exactly once.
    """

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
    grant.save()

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
            applied = self._apply_callback(locked, result)

        return Response({"status": locked.status, "applied": applied})

    def _apply_callback(self, order: Order, result) -> bool:
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
            return False

        if order.status == target:
            # Repeated callback: idempotent no-op.
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
        return True
