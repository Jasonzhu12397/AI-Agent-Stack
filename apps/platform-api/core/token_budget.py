from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import TenantTokenBudget, TenantTokenUsage


class TenantTokenBudgetExceeded(Exception):
    def __init__(self, period: str):
        self.period = period
        super().__init__(f"Tenant {period} token budget would be exceeded")


def _period_usage(tenant_id, start, now):
    settled = TenantTokenUsage.objects.filter(
        tenant_id=tenant_id,
        status=TenantTokenUsage.Status.SETTLED,
        settled_at__gte=start,
        settled_at__lte=now,
    ).aggregate(total=Sum("consumed_tokens"))["total"] or 0
    reserved = TenantTokenUsage.objects.filter(
        tenant_id=tenant_id,
        status=TenantTokenUsage.Status.RESERVED,
        expires_at__gt=now,
    ).aggregate(total=Sum("reserved_tokens"))["total"] or 0
    return settled + reserved


def get_tenant_token_usage(tenant_id, now=None):
    current_time = now or timezone.now()
    local_now = timezone.localtime(current_time)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return {
        "daily_used": _period_usage(tenant_id, day_start, current_time),
        "monthly_used": _period_usage(tenant_id, month_start, current_time),
    }


@transaction.atomic
def reserve_tenant_tokens(
    *, task, idempotency_key: str, requested_tokens: int, agent_run=None, workflow_run=None
):
    if (
        not isinstance(requested_tokens, int)
        or isinstance(requested_tokens, bool)
        or requested_tokens <= 0
    ):
        raise ValueError("Token reservation must be a positive integer")
    if (agent_run is None) == (workflow_run is None):
        raise ValueError("Exactly one AgentRun or WorkflowRun must own a token reservation")
    now = timezone.now()
    lease_expires_at = task.lease_expires_at
    if lease_expires_at is not None and lease_expires_at <= now:
        raise ValueError("Cannot reserve tokens for an expired task lease")
    reservation_expires_at = lease_expires_at or now + timedelta(minutes=5)
    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(
        tenant_id=task.tenant_id,
    )
    usage, created = TenantTokenUsage.objects.select_for_update().get_or_create(
        tenant_id=task.tenant_id,
        idempotency_key=idempotency_key,
        defaults={
            "task": task,
            "agent_run": agent_run,
            "workflow_run": workflow_run,
            "status": TenantTokenUsage.Status.RELEASED,
            "reserved_tokens": 0,
            "expires_at": now,
        },
    )
    if (
        usage.task_id != task.id
        or usage.agent_run_id != (agent_run.id if agent_run is not None else None)
        or usage.workflow_run_id != (workflow_run.id if workflow_run is not None else None)
    ):
        raise ValueError("Token reservation idempotency key belongs to another run")
    if usage.status == TenantTokenUsage.Status.SETTLED:
        raise ValueError("A settled provider call cannot be replayed with the same idempotency key")
    if not created and usage.status == TenantTokenUsage.Status.RESERVED and usage.expires_at > now:
        if usage.reserved_tokens != requested_tokens:
            raise ValueError("Token reservation amount changed for an existing provider call")
        usage.expires_at = reservation_expires_at
        usage.save(update_fields=["expires_at", "updated_at"])
        return usage

    local_now = timezone.localtime(now)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    daily_used = _period_usage(task.tenant_id, day_start, now)
    monthly_used = _period_usage(task.tenant_id, month_start, now)
    if daily_used + requested_tokens > budget.daily_limit:
        raise TenantTokenBudgetExceeded("daily")
    if monthly_used + requested_tokens > budget.monthly_limit:
        raise TenantTokenBudgetExceeded("monthly")

    usage.task = task
    usage.agent_run = agent_run
    usage.workflow_run = workflow_run
    usage.status = TenantTokenUsage.Status.RESERVED
    usage.reserved_tokens = requested_tokens
    usage.consumed_tokens = 0
    usage.expires_at = reservation_expires_at
    usage.created_at = now
    usage.save()
    return usage


@transaction.atomic
def settle_tenant_tokens(*, usage, consumed_tokens: int):
    if (
        not isinstance(consumed_tokens, int)
        or isinstance(consumed_tokens, bool)
        or consumed_tokens < 0
    ):
        raise ValueError("Consumed token count must be a non-negative integer")
    locked = TenantTokenUsage.objects.select_for_update().get(pk=usage.pk)
    if locked.status == TenantTokenUsage.Status.SETTLED:
        if locked.consumed_tokens != consumed_tokens:
            raise ValueError("Settled token usage cannot be changed")
        return locked
    if locked.status != TenantTokenUsage.Status.RESERVED:
        raise ValueError("Only active token reservations can be settled")
    if consumed_tokens > locked.reserved_tokens:
        raise ValueError("Consumed tokens exceed the reserved provider-call ceiling")
    locked.status = TenantTokenUsage.Status.SETTLED
    locked.consumed_tokens = consumed_tokens
    locked.reserved_tokens = 0
    locked.settled_at = timezone.now()
    locked.save(
        update_fields=[
            "status",
            "consumed_tokens",
            "reserved_tokens",
            "settled_at",
            "updated_at",
        ]
    )
    return locked


@transaction.atomic
def release_tenant_tokens(*, usage):
    locked = TenantTokenUsage.objects.select_for_update().get(pk=usage.pk)
    if locked.status in {TenantTokenUsage.Status.RELEASED, TenantTokenUsage.Status.SETTLED}:
        return locked
    locked.status = TenantTokenUsage.Status.RELEASED
    locked.reserved_tokens = 0
    locked.save(update_fields=["status", "reserved_tokens", "updated_at"])
    return locked


def resolve_provider_token_usage(
    *,
    provider_type: str,
    reported_input_tokens: int,
    reported_output_tokens: int,
    input_token_ceiling: int,
    output_token_ceiling: int,
) -> tuple[int, int]:
    values = (
        reported_input_tokens,
        reported_output_tokens,
        input_token_ceiling,
        output_token_ceiling,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise ValueError("Provider token usage and ceilings must be non-negative integers")
    if provider_type == "fake":
        return 0, 0
    return (
        reported_input_tokens or input_token_ceiling,
        reported_output_tokens or output_token_ceiling,
    )
