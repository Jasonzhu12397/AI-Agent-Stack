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


def local_day_bounds(now=None):
    """Return ``(day_start, day_end)`` on the local-timezone day boundary.

    Shares the exact daily boundary used by :func:`get_tenant_token_usage`;
    no second time convention is introduced for the usage payload.
    """
    current_time = now or timezone.now()
    local_now = timezone.localtime(current_time)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


def get_tenant_token_usage(tenant_id, now=None):
    current_time = now or timezone.now()
    day_start, _ = local_day_bounds(current_time)
    month_start = day_start.replace(day=1)
    return {
        "daily_used": _period_usage(tenant_id, day_start, current_time),
        "monthly_used": _period_usage(tenant_id, month_start, current_time),
    }


def _usage_source_ref(row: TenantTokenUsage) -> str:
    if row.scope_ref:
        return row.scope_ref
    if row.agent_run_id:
        return "agent"
    return "workflow"


def get_tenant_usage_history(tenant_id):
    """Settled consumption grouped by local month and recorded source.

    Only settled ledger rows contribute: reserved or released reservations
    are not projected into the history. Every ``consumed_tokens`` value is
    the real ledger total aggregated per ``(period, source)``; it is never
    estimated, copied from the daily/monthly snapshot, or fabricated for an
    empty ledger.
    """
    rows = (
        TenantTokenUsage.objects.filter(
            tenant_id=tenant_id,
            status=TenantTokenUsage.Status.SETTLED,
        )
        .exclude(settled_at__isnull=True)
        .only(
            "scope_ref",
            "agent_run_id",
            "workflow_run_id",
            "consumed_tokens",
            "settled_at",
        )
    )
    grouped: dict[tuple[str, str], int] = {}
    for row in rows:
        period = timezone.localtime(row.settled_at).strftime("%Y-%m")
        source = _usage_source_ref(row)
        grouped[(period, source)] = grouped.get((period, source), 0) + int(
            row.consumed_tokens
        )
    return [
        {"period": period, "source": source, "consumed_tokens": consumed}
        for (period, source), consumed in sorted(
            grouped.items(), key=lambda kv: kv[0][0], reverse=True
        )
    ]


@transaction.atomic
def reserve_tenant_tokens(
    *, task, idempotency_key: str, requested_tokens: int, agent_run=None, workflow_run=None
):
    _assert_positive_token_count(requested_tokens)
    if (agent_run is None) == (workflow_run is None):
        raise ValueError("Exactly one AgentRun or WorkflowRun must own a token reservation")
    _assert_valid_idempotency_key(idempotency_key)
    if task.project.tenant_id != task.tenant_id:
        raise ValueError("Token reservation task project is outside its tenant")
    if agent_run is not None:
        if (
            agent_run.task_id != task.id
            or agent_run.agent_version.agent.tenant_id != task.tenant_id
            or agent_run.agent_version.agent.project_id != task.project_id
        ):
            raise ValueError("AgentRun is outside the token reservation task scope")
    if workflow_run is not None:
        workflow = workflow_run.workflow_version.workflow
        if (
            workflow_run.task_id != task.id
            or workflow.tenant_id != task.tenant_id
            or workflow.project_id != task.project_id
        ):
            raise ValueError("WorkflowRun is outside the token reservation task scope")
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


def _assert_positive_token_count(requested_tokens) -> None:
    if (
        not isinstance(requested_tokens, int)
        or isinstance(requested_tokens, bool)
        or requested_tokens <= 0
    ):
        raise ValueError("Token reservation must be a positive integer")


def _assert_valid_idempotency_key(idempotency_key: str) -> None:
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 160
    ):
        raise ValueError("Token reservation idempotency key must contain 1 to 160 characters")


@transaction.atomic
def reserve_adhoc_tokens(
    *,
    tenant,
    scope_ref: str,
    idempotency_key: str,
    requested_tokens: int,
):
    """Reserve budget for a provider call without an agent/workflow run context.

    Model connection tests and RAG embedding calls have no Task/Run row of their
    own; the ledger row is tied to a free-form ``scope_ref`` instead.
    """
    _assert_positive_token_count(requested_tokens)
    _assert_valid_idempotency_key(idempotency_key)
    if not isinstance(scope_ref, str) or not scope_ref.strip() or len(scope_ref) > 160:
        raise ValueError(
            "Ad-hoc token reservation scope reference must contain 1 to 160 characters"
        )
    now = timezone.now()
    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(tenant=tenant)
    usage, created = TenantTokenUsage.objects.select_for_update().get_or_create(
        tenant=tenant,
        idempotency_key=idempotency_key,
        defaults={
            "scope_ref": scope_ref,
            "status": TenantTokenUsage.Status.RELEASED,
            "reserved_tokens": 0,
            "expires_at": now,
        },
    )
    if usage.scope_ref != scope_ref:
        raise ValueError("Token reservation idempotency key belongs to another scope")
    if usage.status == TenantTokenUsage.Status.SETTLED:
        raise ValueError("A settled provider call cannot be replayed with the same idempotency key")
    if not created and usage.status == TenantTokenUsage.Status.RESERVED and usage.expires_at > now:
        if usage.reserved_tokens != requested_tokens:
            raise ValueError("Token reservation amount changed for an existing provider call")
        usage.expires_at = now + timedelta(minutes=5)
        usage.save(update_fields=["expires_at", "updated_at"])
        return usage

    local_now = timezone.localtime(now)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    daily_used = _period_usage(tenant.id, day_start, now)
    monthly_used = _period_usage(tenant.id, month_start, now)
    if daily_used + requested_tokens > budget.daily_limit:
        raise TenantTokenBudgetExceeded("daily")
    if monthly_used + requested_tokens > budget.monthly_limit:
        raise TenantTokenBudgetExceeded("monthly")

    usage.task = None
    usage.agent_run = None
    usage.workflow_run = None
    usage.scope_ref = scope_ref
    usage.status = TenantTokenUsage.Status.RESERVED
    usage.reserved_tokens = requested_tokens
    usage.consumed_tokens = 0
    usage.expires_at = now + timedelta(minutes=5)
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
