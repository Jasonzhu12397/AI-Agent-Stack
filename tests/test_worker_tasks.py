from datetime import timedelta

import pytest
from core.models import Project, Task, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.utils import timezone
from worker_app.tasks import (
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
