"""Billing domain models: plans, subscriptions, orders and entitlement grants.

The state machine and uniqueness rules here are frozen by the settings/billing
contract (section 3). Entitlement grants are append-only: there is no update
path, and a grant is the only record allowed to move a tenant's token budget.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Plan(models.Model):
    class Interval(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    class RegionScope(models.TextChoices):
        DOMESTIC = "domestic", "Domestic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    interval = models.CharField(
        max_length=16,
        choices=Interval.choices,
        default=Interval.MONTHLY,
    )
    price_cents = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=8, default="CNY")
    token_quota_daily = models.PositiveBigIntegerField(default=0)
    token_quota_monthly = models.PositiveBigIntegerField(default=0)
    features = models.JSONField(default=list)
    region_scope = models.CharField(
        max_length=16,
        choices=RegionScope.choices,
        default=RegionScope.DOMESTIC,
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["price_cents", "code"]

    def __str__(self) -> str:
        return self.code


class Subscription(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    class RenewalMode(models.TextChoices):
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    renewal_mode = models.CharField(
        max_length=16,
        choices=RenewalMode.choices,
        default=RenewalMode.MANUAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "plan"],
                name="uniq_subscription_tenant_plan",
            )
        ]


class Order(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CLOSED = "closed", "Closed"
        REFUNDED = "refunded", "Refunded"

    class Channel(models.TextChoices):
        WECHAT = "wechat", "WeChat Pay"
        ALIPAY = "alipay", "Alipay"

    # Allowed forward transitions. Repeated callbacks for a terminal state are
    # idempotent no-ops; anything else moves state backwards and is rejected.
    TRANSITIONS = {
        Status.CREATED: {Status.PENDING, Status.FAILED, Status.CLOSED},
        Status.PENDING: {Status.PAID, Status.FAILED, Status.CLOSED},
        Status.PAID: {Status.REFUNDED},
        Status.FAILED: set(),
        Status.CLOSED: set(),
        Status.REFUNDED: set(),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="orders",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,
    )
    amount_cents = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=8, default="CNY")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    provider_trade_no = models.CharField(max_length=128, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=160)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_order_idempotency_per_tenant",
            ),
            models.UniqueConstraint(
                fields=["channel", "provider_trade_no"],
                condition=models.Q(provider_trade_no__gt=""),
                name="uniq_order_provider_trade_no",
            ),
        ]

    def can_transition(self, target: str) -> bool:
        return target in self.TRANSITIONS.get(self.status, set())

    def transition(self, target: str) -> None:
        """Move to ``target`` when the state machine allows it.

        Raises ``ValueError`` for backwards or otherwise illegal moves.
        """

        if target == self.status:
            return
        if not self.can_transition(target):
            raise ValueError(
                f"Order {self.id} cannot transition from {self.status} to {target}"
            )
        self.status = target
        if target == self.Status.PAID and self.paid_at is None:
            self.paid_at = timezone.now()
        self.save()


class EntitlementGrant(models.Model):
    """Append-only ledger row crediting token quota to a tenant.

    The budget limit may only move as a consequence of a grant row; there is
    deliberately no update or delete path on this model.
    """

    class Period(models.TextChoices):
        DAILY = "daily", "Daily"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="entitlement_grants",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="grants",
        null=True,
        blank=True,
    )
    order = models.OneToOneField(
        Order,
        on_delete=models.PROTECT,
        related_name="grant",
    )
    period = models.CharField(max_length=16, choices=Period.choices)
    tokens = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Entitlement grants are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Entitlement grants cannot be deleted through the model API.")


class PaymentTransactionAudit(models.Model):
    """Append-only ledger row for provider requests and callbacks.

    Every outbound payment request and every verified or rejected callback is
    recorded here alongside the chain audit event. The metadata dict is
    sanitized upstream: it carries channel, trade numbers and amounts only,
    never private keys, symmetric keys, certificates or PEM material.
    """

    class Direction(models.TextChoices):
        REQUEST = "request", "Request"
        CALLBACK = "callback", "Callback"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.SET_NULL,
        related_name="payment_transaction_audits",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(
        "Order",
        on_delete=models.SET_NULL,
        related_name="payment_transaction_audits",
        null=True,
        blank=True,
    )
    channel = models.CharField(max_length=16)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    event_type = models.CharField(max_length=80)
    status = models.CharField(max_length=32, blank=True, default="")
    amount_cents = models.PositiveBigIntegerField(default=0)
    provider_trade_no = models.CharField(max_length=128, blank=True, default="")
    error_code = models.CharField(max_length=80, blank=True, default="")
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Payment audit rows are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Payment audit rows cannot be deleted through the model API.")
