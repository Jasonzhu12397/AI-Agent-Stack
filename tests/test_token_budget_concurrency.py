"""Concurrency proof that token reservations never double-spend the tenant budget.

The allocation check and the row update run inside one ``select_for_update``
transaction. These tests drive two threads through that critical section with a
barrier so the race is real instead of sleeping against a scheduler.
"""

import threading
import time

import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    Project,
    Task,
    Tenant,
    TenantTokenBudget,
    TenantTokenUsage,
)
from core.token_budget import (
    TenantTokenBudgetExceeded,
    release_tenant_tokens,
    reserve_tenant_tokens,
    settle_tenant_tokens,
)
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.db.utils import OperationalError
from django.utils import timezone

RETRY_ATTEMPTS = 10
RETRY_BACKOFF_SECONDS = 0.02


def _is_postgres() -> bool:
    return settings.DATABASES["default"]["ENGINE"].endswith("postgresql")


@pytest.fixture
def budget_run(db):
    user = get_user_model().objects.create_user(username="concurrency-budget-user")
    tenant = Tenant.objects.create(name="Concurrency tenant", slug="concurrency-tenant")
    project = Project.objects.create(
        tenant=tenant, name="Concurrency project", slug="concurrency-project"
    )
    task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="agent.run",
        idempotency_key="concurrency-budget-task",
        available_at=timezone.now(),
    )
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Concurrency agent",
        slug="concurrency-agent",
    )
    version = AgentVersion.objects.create(
        agent=agent, version=1, config_snapshot={}, created_by=user
    )
    run = AgentRun.objects.create(task=task, agent_version=version, input_text="hello")
    budget = TenantTokenBudget.objects.create(tenant=tenant, daily_limit=100, monthly_limit=100)
    return task, run, budget


def _reserve_with_retry(task, run, key, requested_tokens, barrier, results, failures):
    barrier.wait()
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            usage = reserve_tenant_tokens(
                task=task,
                agent_run=run,
                idempotency_key=key,
                requested_tokens=requested_tokens,
            )
            results.append(usage)
            return
        except TenantTokenBudgetExceeded as exc:
            failures.append(exc)
            return
        except OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == RETRY_ATTEMPTS - 1:
                failures.append(exc)
                return
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    failures.append(last_error)


def _run_two_threads(task, run, requested_tokens, key_prefix):
    barrier = threading.Barrier(2)
    results: list = []
    failures: list = []
    threads = [
        threading.Thread(
            target=_reserve_with_retry,
            args=(task, run, f"{key_prefix}:{index}", requested_tokens, barrier, results, failures),
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, failures


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservation_allocates_remaining_once(budget_run):
    task, run, budget = budget_run
    budget.daily_limit = 100
    budget.monthly_limit = 100
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])

    results, failures = _run_two_threads(task, run, 60, "once")

    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TenantTokenBudgetExceeded)
    assert failures[0].period == "daily"

    reserved = TenantTokenUsage.objects.filter(
        tenant=task.tenant, status=TenantTokenUsage.Status.RESERVED
    ).aggregate(total=Sum("reserved_tokens"))["total"]
    assert reserved == 60


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservation_no_double_spend(budget_run):
    task, run, budget = budget_run
    budget.daily_limit = 200
    budget.monthly_limit = 200
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])

    results, failures = _run_two_threads(task, run, 60, "shared")

    assert failures == []
    assert len(results) == 2

    rows = TenantTokenUsage.objects.filter(tenant=task.tenant)
    reserved = sum(row.reserved_tokens for row in rows)
    consumed = sum(row.consumed_tokens for row in rows)
    assert reserved + consumed <= budget.daily_limit
    assert reserved + consumed <= budget.monthly_limit


@pytest.mark.django_db
def test_settle_beyond_reservation_rejected(budget_run):
    task, run, _ = budget_run
    reservation = reserve_tenant_tokens(
        task=task, agent_run=run, idempotency_key="over-ceiling-concurrency", requested_tokens=40
    )

    with pytest.raises(ValueError, match="exceed the reserved provider-call ceiling"):
        settle_tenant_tokens(usage=reservation, consumed_tokens=41)

    reservation.refresh_from_db()
    assert reservation.status == TenantTokenUsage.Status.RESERVED
    assert reservation.reserved_tokens == 40
    assert reservation.consumed_tokens == 0


@pytest.mark.django_db
def test_release_restores_capacity(budget_run):
    task, run, budget = budget_run
    budget.daily_limit = 100
    budget.monthly_limit = 100
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])

    first = reserve_tenant_tokens(
        task=task, agent_run=run, idempotency_key="release-1", requested_tokens=60
    )
    with pytest.raises(TenantTokenBudgetExceeded):
        reserve_tenant_tokens(
            task=task, agent_run=run, idempotency_key="release-2", requested_tokens=60
        )

    release_tenant_tokens(usage=first)
    first.refresh_from_db()
    assert first.status == TenantTokenUsage.Status.RELEASED
    assert first.reserved_tokens == 0

    retry = reserve_tenant_tokens(
        task=task, agent_run=run, idempotency_key="release-3", requested_tokens=60
    )
    assert retry.reserved_tokens == 60


@pytest.mark.skipif(not _is_postgres(), reason="PostgreSQL row-lock proof runs on CI only")
@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_reservation_allocates_remaining_once(budget_run):
    task, run, budget = budget_run
    budget.daily_limit = 100
    budget.monthly_limit = 100
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])

    results, failures = _run_two_threads(task, run, 60, "pg-once")

    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TenantTokenBudgetExceeded)
