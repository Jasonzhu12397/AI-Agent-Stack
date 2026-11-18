import json
from datetime import timedelta

import pytest
from core.models import Project, Task, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.utils import timezone
from worker_app.tasks import (
    _allowed_agent_output_tokens,
    _resolve_and_settle_provider_usage,
    PermanentTaskError,
    claim_next_task,
    fail_task,
    finish_task,
    process_once,
    renew_lease,
)


@pytest.fixture
def queued_task(db):
    user = get_user_model().objects.create_user(
        username="worker-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Worker tenant", slug="worker-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(tenant=tenant, name="Worker project", slug="worker-project")
    return Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="demo.echo",
        payload={"message": "durable"},
        idempotency_key="worker-task-001",
        available_at=timezone.now(),
    )


@pytest.mark.django_db
def test_worker_claims_and_completes_persisted_task(queued_task):
    processed = process_once("worker-test-1")
    queued_task.refresh_from_db()

    assert processed is True
    assert queued_task.status == Task.Status.SUCCEEDED
    assert queued_task.attempts == 1
    assert queued_task.result == {"echo": {"message": "durable"}}
    assert queued_task.finished_at is not None


@pytest.mark.django_db
def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finish(queued_task):
    first = claim_next_task("worker-old", lease_seconds=1)
    old_token = first.lease_token
    Task.objects.filter(pk=queued_task.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    second = claim_next_task("worker-new", lease_seconds=30)
    assert second is not None
    assert second.attempts == 2
    assert second.lease_token != old_token
    assert finish_task(queued_task.id, old_token, {"stale": True}) is False
    assert finish_task(queued_task.id, second.lease_token, {"recovered": True}) is True

    queued_task.refresh_from_db()
    assert queued_task.status == Task.Status.SUCCEEDED
    assert queued_task.result == {"recovered": True}


@pytest.mark.django_db
def test_worker_fails_task_after_unsupported_type(queued_task):
    Task.objects.filter(pk=queued_task.pk).update(task_type="not.registered")
    process_once("worker-test-2")
    queued_task.refresh_from_db()

    assert queued_task.status == Task.Status.FAILED
    assert queued_task.error["code"] == "TASK_VALIDATION_FAILED"
    assert queued_task.attempts == 1



@pytest.mark.django_db
def test_expired_worker_cannot_renew_or_finish_before_reclaim(queued_task):
    first = claim_next_task("worker-expired", lease_seconds=30)
    old_token = first.lease_token
    Task.objects.filter(pk=queued_task.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    assert renew_lease(queued_task.id, old_token, "worker-expired") is False
    assert finish_task(queued_task.id, old_token, {"stale": True}) is False

    queued_task.refresh_from_db()
    assert queued_task.status == Task.Status.RUNNING
    assert queued_task.lease_token == old_token

    second = claim_next_task("worker-recovered", lease_seconds=30)
    assert second is not None
    assert second.lease_token != old_token
    assert finish_task(queued_task.id, second.lease_token, {"recovered": True}) is True


@pytest.mark.django_db
def test_expired_worker_cannot_fail_task_before_reclaim(queued_task):
    first = claim_next_task("worker-expired-failure", lease_seconds=30)
    old_token = first.lease_token
    Task.objects.filter(pk=queued_task.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    assert fail_task(
        queued_task.id,
        old_token,
        {"code": "STALE_WORKER_FAILURE"},
        retryable=False,
    ) is False

    queued_task.refresh_from_db()
    assert queued_task.status == Task.Status.RUNNING
    assert queued_task.lease_token == old_token

    second = claim_next_task("worker-recovered-failure", lease_seconds=30)
    assert second is not None
    assert second.lease_token != old_token


def test_agent_token_reservation_uses_utf8_byte_upper_bound():
    from worker_app.tasks import (
        _estimate_message_token_upper_bound,
        _estimate_message_tokens,
    )

    messages = [{"role": "user", "content": "你好，部署 SR-IOV"}]
    estimated = _estimate_message_tokens(messages)
    upper_bound = _estimate_message_token_upper_bound(messages)

    serialized = json.dumps(
        {"messages": messages, "tools": []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert upper_bound == len(serialized)
    assert upper_bound >= estimated


def test_agent_run_output_budget_uses_conservative_input_ceiling():
    assert _allowed_agent_output_tokens(
        2048,
        4096,
        0,
        0,
        15000,
    ) == 1000



def test_invalid_provider_usage_settles_the_full_reserved_ceiling(monkeypatch):
    from types import SimpleNamespace
    import worker_app.tasks as worker_tasks

    reservation = SimpleNamespace(reserved_tokens=1200)
    settlements = []

    def reject_usage(**kwargs):
        raise PermanentTaskError("invalid provider usage")

    monkeypatch.setattr(worker_tasks, "_resolve_provider_usage", reject_usage)
    monkeypatch.setattr(
        worker_tasks,
        "_settle_provider_usage",
        lambda usage, consumed_tokens: settlements.append((usage, consumed_tokens)),
    )

    with pytest.raises(PermanentTaskError, match="invalid provider usage"):
        _resolve_and_settle_provider_usage(
            token_reservation=reservation,
            provider_type="openai_compatible",
            reported_input_tokens=-1,
            reported_output_tokens=0,
            input_token_ceiling=1000,
            output_token_ceiling=200,
        )

    assert settlements == [(reservation, 1200)]


def test_valid_provider_usage_settles_actual_consumption(monkeypatch):
    from types import SimpleNamespace
    import worker_app.tasks as worker_tasks

    reservation = SimpleNamespace(reserved_tokens=1200)
    settlements = []
    monkeypatch.setattr(
        worker_tasks,
        "_resolve_provider_usage",
        lambda **kwargs: (700, 100),
    )
    monkeypatch.setattr(
        worker_tasks,
        "_settle_provider_usage",
        lambda usage, consumed_tokens: settlements.append((usage, consumed_tokens)),
    )

    result = _resolve_and_settle_provider_usage(
        token_reservation=reservation,
        provider_type="openai_compatible",
        reported_input_tokens=700,
        reported_output_tokens=100,
        input_token_ceiling=1000,
        output_token_ceiling=200,
    )

    assert result == (700, 100)
    assert settlements == [(reservation, 800)]
