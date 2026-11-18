import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import (
    AgentDefinition, AgentRun, AgentVersion, Project, Task, Tenant,
    TenantTokenBudget, TenantTokenUsage,
)
from core.token_budget import (
    TenantTokenBudgetExceeded, release_tenant_tokens,
    reserve_tenant_tokens, settle_tenant_tokens,
)


@pytest.fixture
def token_budget_run(db):
    user = get_user_model().objects.create_user(username="token-budget-user")
    tenant = Tenant.objects.create(name="Token budget tenant", slug="token-budget-tenant")
    project = Project.objects.create(tenant=tenant, name="Token project", slug="token-project")
    task = Task.objects.create(
        tenant=tenant, project=project, created_by=user, task_type="agent.run",
        idempotency_key="token-budget-task", available_at=timezone.now(),
    )
    agent = AgentDefinition.objects.create(
        tenant=tenant, project=project, created_by=user, name="Token agent", slug="token-agent",
    )
    version = AgentVersion.objects.create(
        agent=agent, version=1, config_snapshot={}, created_by=user,
    )
    run = AgentRun.objects.create(task=task, agent_version=version, input_text="hello")
    budget = TenantTokenBudget.objects.create(tenant=tenant, daily_limit=100, monthly_limit=150)
    return task, run, budget


@pytest.mark.django_db
def test_tenant_token_budget_reserves_before_provider_usage_and_rejects_over_limit(token_budget_run):
    task, run, _ = token_budget_run
    reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:0", requested_tokens=80)
    with pytest.raises(TenantTokenBudgetExceeded) as error:
        reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:1", requested_tokens=30)
    assert error.value.period == "daily"


@pytest.mark.django_db
def test_tenant_token_budget_settlement_releases_unused_reservation(token_budget_run):
    task, run, _ = token_budget_run
    reservation = reserve_tenant_tokens(
        task=task, agent_run=run, idempotency_key="run:1:0", requested_tokens=80
    )
    settle_tenant_tokens(usage=reservation, consumed_tokens=35)
    reservation.refresh_from_db()
    assert reservation.status == TenantTokenUsage.Status.SETTLED
    assert reservation.reserved_tokens == 0
    assert reservation.consumed_tokens == 35
    reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:1", requested_tokens=65)


@pytest.mark.django_db
def test_tenant_token_budget_reservation_is_idempotent_and_releasable(token_budget_run):
    task, run, _ = token_budget_run
    first = reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:0", requested_tokens=40)
    second = reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:0", requested_tokens=40)
    assert first.pk == second.pk
    release_tenant_tokens(usage=first)
    first.refresh_from_db()
    assert first.status == TenantTokenUsage.Status.RELEASED
    assert first.reserved_tokens == 0
