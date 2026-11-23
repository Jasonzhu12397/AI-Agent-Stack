"""Subscription lifecycle service: trial, cancel, renew and expiry settlement.

Every subscription state move goes through this module; HTTP views never mutate
subscription rows directly. Paid quota is credited only through an
:class:`~core.billing_models.EntitlementGrant` row. Expiry settlement resets the
tenant budget to the plan's post-expiry fallback quota (zero when the plan
defines none) and records the previous limits in an append-only audit event, so
the budget change is always traceable rather than an anonymous number edit.

State machine (terminal states first)::

    pending -> trialing | active
    trialing -> active | expired
    active -> past_due | cancelled | expired
    past_due -> active | cancelled | expired
    cancelled / expired -> (terminal)
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.audit import record_audit_event
from core.billing_models import EntitlementGrant, Order, Plan, Subscription
from core.models import AuditEvent, TenantTokenBudget


class SubscriptionStateError(Exception):
    """Contract-level lifecycle failure mapped onto the error envelope."""

    def __init__(
        self, code: str, message: str, http_status: int = 409, details: dict | None = None
    ):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        super().__init__(message)


ACTIVE_STATES = {
    Subscription.Status.TRIALING,
    Subscription.Status.ACTIVE,
    Subscription.Status.PAST_DUE,
}

_PERIOD_LENGTH = {
    Plan.Interval.MONTHLY: timedelta(days=30),
    Plan.Interval.YEARLY: timedelta(days=365),
}


def _period_delta(plan: Plan) -> timedelta:
    return _PERIOD_LENGTH.get(plan.interval, timedelta(days=30))


def _apply_plan_quota(tenant, plan: Plan) -> TenantTokenBudget:
    """Set the budget to the plan's paid/trial quota through a locked row."""

    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(tenant=tenant)
    budget.daily_limit = plan.token_quota_daily
    budget.monthly_limit = plan.token_quota_monthly
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])
    return budget


def _apply_expired_quota(tenant, plan: Plan) -> tuple[TenantTokenBudget, dict]:
    """Reset the budget to the plan's post-expiry fallback quota.

    Returns the budget row and the previous limits so the caller can record
    them in the audit metadata.
    """

    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(tenant=tenant)
    previous = {"daily_limit": budget.daily_limit, "monthly_limit": budget.monthly_limit}
    budget.daily_limit = plan.token_quota_daily_expired
    budget.monthly_limit = plan.token_quota_monthly_expired
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])
    return budget, previous


@transaction.atomic
def start_trial(*, tenant, plan: Plan, request=None, actor=None) -> Subscription:
    """Open a free trial for a plan that defines a positive ``trial_days``."""

    if not plan.active:
        raise SubscriptionStateError(
            "SUBSCRIPTION_PLAN_UNAVAILABLE",
            "The selected plan is not available.",
            http_status=400,
        )
    if not plan.trial_days or plan.trial_days <= 0:
        raise SubscriptionStateError(
            "SUBSCRIPTION_PLAN_NO_TRIAL",
            "The selected plan does not offer a free trial.",
            http_status=400,
            details={"plan_code": plan.code},
        )
    active = (
        Subscription.objects.select_for_update()
        .filter(tenant=tenant, status__in=ACTIVE_STATES)
        .first()
    )
    if active is not None:
        raise SubscriptionStateError(
            "SUBSCRIPTION_ALREADY_ACTIVE",
            "The tenant already has an active subscription.",
            http_status=409,
            details={"existing_subscription": str(active.id), "existing_status": active.status},
        )

    now = timezone.now()
    subscription = Subscription.objects.create(
        tenant=tenant,
        plan=plan,
        status=Subscription.Status.TRIALING,
        started_at=now,
        current_period_start=now,
        current_period_end=now + timedelta(days=plan.trial_days),
    )
    _apply_plan_quota(tenant, plan)
    record_audit_event(
        request,
        tenant,
        "billing.subscription.trial_started",
        "subscription",
        subscription.id,
        actor=actor,
        metadata={"plan_code": plan.code, "trial_days": plan.trial_days},
    )
    return subscription


@transaction.atomic
def cancel_subscription(
    *,
    subscription: Subscription,
    mode: str,
    request=None,
    actor=None,
) -> Subscription:
    """Cancel a subscription.

    ``immediate`` moves straight to ``cancelled``; ``period_end`` only sets the
    cancellation flag and the subscription keeps serving until settlement
    moves it to ``cancelled`` after ``current_period_end``.
    """

    if mode not in {"immediate", "period_end"}:
        raise SubscriptionStateError(
            "SUBSCRIPTION_CANCEL_MODE_INVALID",
            "Cancel mode must be 'immediate' or 'period_end'.",
            http_status=400,
            details={"mode": [mode]},
        )
    if subscription.status in {Subscription.Status.CANCELLED, Subscription.Status.EXPIRED}:
        raise SubscriptionStateError(
            "SUBSCRIPTION_INVALID_TRANSITION",
            f"Subscription in status {subscription.status} cannot be cancelled.",
            http_status=409,
            details={"status": subscription.status},
        )

    locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if mode == "immediate":
        locked.transition(Subscription.Status.CANCELLED)
        record_audit_event(
            request,
            locked.tenant,
            "billing.subscription.cancelled_immediately",
            "subscription",
            locked.id,
            actor=actor,
            metadata={"plan_code": locked.plan.code},
        )
        return locked

    locked.cancel_at_period_end = True
    locked.save(update_fields=["cancel_at_period_end", "updated_at"])
    record_audit_event(
        request,
        locked.tenant,
        "billing.subscription.cancel_at_period_end",
        "subscription",
        locked.id,
        actor=actor,
        metadata={
            "plan_code": locked.plan.code,
            "effective_at": locked.current_period_end.isoformat()
            if locked.current_period_end
            else None,
        },
    )
    return locked


@transaction.atomic
def renew_subscription(
    *,
    subscription: Subscription,
    order: Order,
    request=None,
    actor=None,
) -> tuple[Subscription, EntitlementGrant]:
    """Extend the current period and credit the plan quota through a grant.

    A renewal grant is issued at most once per period: a second renewal while a
    grant still covers the current period is rejected instead of stacking quota.
    """

    if order.tenant_id != subscription.tenant_id:
        raise SubscriptionStateError(
            "SUBSCRIPTION_RENEWAL_ORDER_MISMATCH",
            "The renewal order belongs to another tenant.",
            http_status=400,
        )
    if order.plan_id != subscription.plan_id:
        raise SubscriptionStateError(
            "SUBSCRIPTION_RENEWAL_ORDER_MISMATCH",
            "The renewal order does not match the subscription plan.",
            http_status=400,
        )
    if order.status != Order.Status.PAID:
        raise SubscriptionStateError(
            "ORDER_STATE_CONFLICT",
            f"Order in status {order.status} cannot renew a subscription.",
            http_status=409,
            details={"order_status": order.status},
        )

    locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if locked.status in {Subscription.Status.CANCELLED, Subscription.Status.EXPIRED}:
        raise SubscriptionStateError(
            "SUBSCRIPTION_INVALID_TRANSITION",
            f"Subscription in status {locked.status} cannot be renewed.",
            http_status=409,
            details={"status": locked.status},
        )

    now = timezone.now()
    prior = (
        EntitlementGrant.objects.filter(subscription=locked, expires_at__gt=now)
        .order_by("-created_at")
        .first()
    )
    if prior is not None:
        raise SubscriptionStateError(
            "SUBSCRIPTION_GRANT_ALREADY_ISSUED",
            "Quota for the current period was already issued; renew after the period lapses.",
            http_status=409,
            details={"grant_id": str(prior.id)},
        )

    plan = locked.plan
    period_end = locked.current_period_end
    base = period_end if period_end and period_end > now else now
    locked.current_period_start = now
    locked.current_period_end = base + _period_delta(plan)
    locked.cancel_at_period_end = False
    if locked.status in {Subscription.Status.TRIALING, Subscription.Status.PAST_DUE}:
        locked.transition(Subscription.Status.ACTIVE)
    else:
        locked.status = Subscription.Status.ACTIVE
    locked.save(
        update_fields=[
            "current_period_start",
            "current_period_end",
            "cancel_at_period_end",
            "status",
            "updated_at",
        ]
    )

    grant = EntitlementGrant(
        tenant=locked.tenant,
        subscription=locked,
        order=order,
        period=EntitlementGrant.Period.MONTHLY,
        tokens=plan.token_quota_monthly,
        expires_at=locked.current_period_end,
        reason=f"renewal:{plan.code}",
    )
    grant.save()
    _apply_plan_quota(locked.tenant, plan)
    record_audit_event(
        request,
        locked.tenant,
        "billing.subscription.renewed",
        "subscription",
        locked.id,
        actor=actor,
        metadata={
            "plan_code": plan.code,
            "order_id": str(order.id),
            "period_end": locked.current_period_end.isoformat(),
            "tokens": grant.tokens,
        },
    )
    return locked, grant


@transaction.atomic
def settle_due_subscriptions(*, now=None, request=None, tenant=None) -> list[Subscription]:
    """Settle subscriptions whose current period has lapsed.

    A subscription flagged ``cancel_at_period_end`` lands in ``cancelled``; any
    other lapsed subscription lands in ``expired``. The budget is reset to the
    plan's post-expiry fallback quota and the previous limits are audited.
    """

    current_time = now or timezone.now()
    queryset = Subscription.objects.select_for_update().filter(
        current_period_end__lte=current_time,
        status__in=ACTIVE_STATES,
    )
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)

    settled: list[Subscription] = []
    for subscription in queryset:
        plan = subscription.plan
        target = (
            Subscription.Status.CANCELLED
            if subscription.cancel_at_period_end
            else Subscription.Status.EXPIRED
        )
        _budget, previous_limits = _apply_expired_quota(subscription.tenant, plan)
        subscription.transition(target)
        event_type = (
            "billing.subscription.cancelled_at_period_end"
            if target == Subscription.Status.CANCELLED
            else "billing.subscription.expired"
        )
        record_audit_event(
            request,
            subscription.tenant,
            event_type,
            "subscription",
            subscription.id,
            metadata={
                "plan_code": plan.code,
                "previous_daily_limit": previous_limits["daily_limit"],
                "previous_monthly_limit": previous_limits["monthly_limit"],
                "daily_limit": plan.token_quota_daily_expired,
                "monthly_limit": plan.token_quota_monthly_expired,
            },
            outcome=AuditEvent.Outcome.SUCCESS,
        )
        settled.append(subscription)
    return settled
