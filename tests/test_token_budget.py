from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    Project,
    Task,
    Tenant,
    TenantMembership,
    TenantTokenBudget,
    TenantTokenUsage,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from core.token_budget import (
    TenantTokenBudgetExceeded,
    release_tenant_tokens,
    reserve_tenant_tokens,
    resolve_provider_token_usage,
    settle_tenant_tokens,
)
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient


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
def test_tenant_token_budget_rejects_reservations_over_the_daily_limit(token_budget_run):
    task, run, _ = token_budget_run
    reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:0", requested_tokens=80)
    with pytest.raises(TenantTokenBudgetExceeded) as error:
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="run:1:1",
            requested_tokens=30,
        )
    assert error.value.period == "daily"


@pytest.mark.django_db
def test_tenant_token_budget_enforces_monthly_limit_independently(token_budget_run):
    task, run, budget = token_budget_run
    budget.daily_limit = 200
    budget.monthly_limit = 100
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])
    reserve_tenant_tokens(
        task=task,
        agent_run=run,
        idempotency_key="monthly-call-1",
        requested_tokens=80,
    )

    with pytest.raises(TenantTokenBudgetExceeded) as error:
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="monthly-call-2",
            requested_tokens=30,
        )

    assert error.value.period == "monthly"


@pytest.mark.parametrize("invalid_tokens", [0, -1, True, False, "10", 1.5])
@pytest.mark.django_db
def test_tenant_token_budget_rejects_invalid_reservation_amounts(
    token_budget_run, invalid_tokens
):
    task, run, _ = token_budget_run
    with pytest.raises(ValueError, match="positive integer"):
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="invalid-reservation",
            requested_tokens=invalid_tokens,
        )


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
    with pytest.raises(ValueError, match="cannot be replayed"):
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="run:1:0",
            requested_tokens=80,
        )
    reserve_tenant_tokens(task=task, agent_run=run, idempotency_key="run:1:1", requested_tokens=65)


@pytest.mark.django_db
def test_tenant_token_budget_rejects_settlement_above_reserved_ceiling(token_budget_run):
    task, run, _ = token_budget_run
    reservation = reserve_tenant_tokens(
        task=task,
        agent_run=run,
        idempotency_key="over-ceiling-provider-call",
        requested_tokens=40,
    )

    with pytest.raises(ValueError, match="exceed the reserved provider-call ceiling"):
        settle_tenant_tokens(usage=reservation, consumed_tokens=41)

    reservation.refresh_from_db()
    assert reservation.status == TenantTokenUsage.Status.RESERVED
    assert reservation.reserved_tokens == 40
    assert reservation.consumed_tokens == 0


@pytest.mark.django_db
def test_tenant_token_budget_reservation_is_idempotent_and_releasable(token_budget_run):
    task, run, _ = token_budget_run
    first = reserve_tenant_tokens(
        task=task,
        agent_run=run,
        idempotency_key="run:1:0",
        requested_tokens=40,
    )
    second = reserve_tenant_tokens(
        task=task,
        agent_run=run,
        idempotency_key="run:1:0",
        requested_tokens=40,
    )
    assert first.pk == second.pk
    release_tenant_tokens(usage=first)
    first.refresh_from_db()
    assert first.status == TenantTokenUsage.Status.RELEASED
    assert first.reserved_tokens == 0


@pytest.mark.django_db
def test_workflow_model_calls_use_the_same_tenant_budget_ledger():
    user = get_user_model().objects.create_user(username="workflow-token-budget-user")
    tenant = Tenant.objects.create(name="Workflow budget tenant", slug="workflow-budget-tenant")
    project = Project.objects.create(
        tenant=tenant, name="Workflow project", slug="workflow-project"
    )
    task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="workflow.run",
        idempotency_key="workflow-token-budget-task",
        available_at=timezone.now(),
    )
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Budget workflow",
        slug="budget-workflow",
    )
    version = WorkflowVersion.objects.create(
        workflow=workflow,
        version=1,
        definition_snapshot={},
        created_by=user,
    )
    workflow_run = WorkflowRun.objects.create(task=task, workflow_version=version)
    TenantTokenBudget.objects.create(tenant=tenant, daily_limit=100, monthly_limit=100)

    reservation = reserve_tenant_tokens(
        task=task,
        workflow_run=workflow_run,
        idempotency_key="workflow-call-1",
        requested_tokens=70,
    )

    assert reservation.agent_run_id is None
    assert reservation.workflow_run_id == workflow_run.id
    settle_tenant_tokens(usage=reservation, consumed_tokens=22)
    reservation.refresh_from_db()
    assert reservation.consumed_tokens == 22


@pytest.mark.django_db
def test_settled_usage_is_charged_to_the_day_the_provider_call_finishes(token_budget_run):
    task, run, _ = token_budget_run
    reserved_at = datetime(2026, 11, 17, 23, 59, tzinfo=UTC)
    settled_at = reserved_at + timedelta(minutes=2)
    with (
        patch("core.token_budget.timezone.now", return_value=reserved_at),
        patch("core.token_budget.timezone.localtime", side_effect=lambda value: value),
    ):
        reservation = reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="cross-midnight-call",
            requested_tokens=80,
        )

    with (
        patch("core.token_budget.timezone.now", return_value=settled_at),
        patch("core.token_budget.timezone.localtime", side_effect=lambda value: value),
        pytest.raises(TenantTokenBudgetExceeded, match="daily"),
    ):
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="next-day-while-pending",
            requested_tokens=30,
        )

    with patch("core.token_budget.timezone.now", return_value=settled_at):
        settle_tenant_tokens(usage=reservation, consumed_tokens=35)

    reservation.refresh_from_db()
    assert reservation.settled_at == settled_at

    with (
        patch("core.token_budget.timezone.now", return_value=settled_at + timedelta(minutes=1)),
        patch("core.token_budget.timezone.localtime", side_effect=lambda value: value),
        pytest.raises(TenantTokenBudgetExceeded, match="daily"),
    ):
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key="next-day-call",
            requested_tokens=66,
        )


@pytest.mark.parametrize("invalid_limit", [True, False, 1.5, "12"])
@pytest.mark.django_db
def test_tenant_token_budget_api_rejects_non_integer_limits(token_budget_run, invalid_limit):
    task, _, _ = token_budget_run
    TenantMembership.objects.create(
        tenant=task.tenant,
        user=task.created_by,
        role=TenantMembership.Role.OWNER,
    )
    client = APIClient()
    client.force_authenticate(user=task.created_by)
    url = reverse("tenant-token-budget", kwargs={"tenant_id": task.tenant_id})

    response = client.patch(url, {"daily_limit": invalid_limit}, format="json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_tenant_token_budget_api_is_tenant_admin_managed(token_budget_run):
    task, _, budget = token_budget_run
    owner = task.created_by
    TenantMembership.objects.create(
        tenant=task.tenant,
        user=owner,
        role=TenantMembership.Role.OWNER,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    url = reverse("tenant-token-budget", kwargs={"tenant_id": task.tenant_id})

    response = client.patch(url, {"daily_limit": 75}, format="json")
    assert response.status_code == 200
    assert response.data["daily_limit"] == 75
    assert response.data["monthly_limit"] == budget.monthly_limit

    member = get_user_model().objects.create_user(username="token-budget-member")
    TenantMembership.objects.create(
        tenant=task.tenant,
        user=member,
        role=TenantMembership.Role.MEMBER,
    )
    client.force_authenticate(user=member)
    denied = client.patch(url, {"daily_limit": 50}, format="json")
    assert denied.status_code == 403


@pytest.mark.parametrize(
    "provider_type,reported_input,reported_output,expected",
    [
        ("openai_compatible", 0, 0, (1200, 800)),
        ("openai_compatible", 900, 400, (900, 400)),
        ("fake", 0, 0, (0, 0)),
    ],
)
def test_provider_token_usage_falls_back_to_reserved_ceilings(
    provider_type,
    reported_input,
    reported_output,
    expected,
):
    assert resolve_provider_token_usage(
        provider_type=provider_type,
        reported_input_tokens=reported_input,
        reported_output_tokens=reported_output,
        input_token_ceiling=1200,
        output_token_ceiling=800,
    ) == expected


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "12"])
def test_provider_token_usage_rejects_invalid_counts(invalid):
    with pytest.raises(ValueError, match="non-negative integers"):
        resolve_provider_token_usage(
            provider_type="openai_compatible",
            reported_input_tokens=invalid,
            reported_output_tokens=0,
            input_token_ceiling=1200,
            output_token_ceiling=800,
        )



@pytest.mark.django_db
def test_token_reservation_rejects_agent_run_from_another_task(token_budget_run):
    task, run, _ = token_budget_run
    other_task = Task.objects.create(
        tenant=task.tenant,
        project=task.project,
        created_by=task.created_by,
        task_type="agent.run",
        idempotency_key="other-token-budget-task",
        available_at=timezone.now(),
    )

    with pytest.raises(ValueError, match="outside the token reservation task scope"):
        reserve_tenant_tokens(
            task=other_task,
            agent_run=run,
            idempotency_key="cross-task-agent-run",
            requested_tokens=10,
        )


@pytest.mark.django_db
def test_token_reservation_rejects_workflow_run_from_another_task():
    user = get_user_model().objects.create_user(username="workflow-scope-user")
    tenant = Tenant.objects.create(name="Workflow scope tenant", slug="workflow-scope-tenant")
    project = Project.objects.create(tenant=tenant, name="Workflow scope", slug="workflow-scope")
    task = Task.objects.create(
        tenant=tenant, project=project, created_by=user, task_type="workflow.run",
        idempotency_key="workflow-scope-task", available_at=timezone.now(),
    )
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant, project=project, created_by=user, name="Scope workflow",
        slug="scope-workflow",
    )
    version = WorkflowVersion.objects.create(
        workflow=workflow, version=1, definition_snapshot={}, created_by=user,
    )
    workflow_run = WorkflowRun.objects.create(task=task, workflow_version=version)
    other_task = Task.objects.create(
        tenant=tenant, project=project, created_by=user, task_type="workflow.run",
        idempotency_key="other-workflow-scope-task", available_at=timezone.now(),
    )
    TenantTokenBudget.objects.create(tenant=tenant, daily_limit=100, monthly_limit=100)

    with pytest.raises(ValueError, match="outside the token reservation task scope"):
        reserve_tenant_tokens(
            task=other_task,
            workflow_run=workflow_run,
            idempotency_key="cross-task-workflow-run",
            requested_tokens=10,
        )


@pytest.mark.parametrize("key", ["", "   ", "x" * 161, None])
@pytest.mark.django_db
def test_token_reservation_rejects_invalid_idempotency_keys(token_budget_run, key):
    task, run, _ = token_budget_run
    with pytest.raises(ValueError, match="idempotency key"):
        reserve_tenant_tokens(
            task=task,
            agent_run=run,
            idempotency_key=key,
            requested_tokens=10,
        )
