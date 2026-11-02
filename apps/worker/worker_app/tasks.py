import uuid
from datetime import timedelta
from time import sleep

from django.db import transaction
from django.db.models import Q
from django.utils import timezone



class PermanentTaskError(Exception):
    pass


def claim_next_task(worker_id, lease_seconds=30):
    now = timezone.now()
    with transaction.atomic():
        task = (
            Task.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=Task.Status.QUEUED, available_at__lte=now)
                | Q(status=Task.Status.RUNNING, lease_expires_at__lt=now)
                | Q(status=Task.Status.CANCEL_REQUESTED, lease_expires_at__lt=now)
            )
            .order_by("available_at", "created_at")
            .first()
        )
        if task is None:
            return None
        if task.status == Task.Status.CANCEL_REQUESTED:
            task.status = Task.Status.CANCELLED
            task.finished_at = now
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "lease_owner",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return None
        if task.status == Task.Status.RUNNING and task.attempts >= task.max_attempts:
            task.status = Task.Status.FAILED
            task.error = {
                "code": "LEASE_EXPIRED",
                "message": "The task lease expired after the maximum number of attempts.",
                "retryable": False,
            }
            task.finished_at = now
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.save(
                update_fields=[
                    "status",
                    "error",
                    "finished_at",
                    "lease_owner",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return None
        task.status = Task.Status.RUNNING
        task.attempts += 1
        task.lease_owner = worker_id
        task.lease_token = uuid.uuid4()
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.heartbeat_at = now
        if task.started_at is None:
            task.started_at = now
        task.save(
            update_fields=[
                "status",
                "attempts",
                "lease_owner",
                "lease_token",
                "lease_expires_at",
                "heartbeat_at",
                "started_at",
                "updated_at",
            ]
        )
        return task


def renew_lease(task_id, lease_token, worker_id, lease_seconds=30):
    now = timezone.now()
    updated = Task.objects.filter(
        pk=task_id,
        status=Task.Status.RUNNING,
        lease_token=lease_token,
        lease_owner=worker_id,
    ).update(
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        updated_at=now,
    )
    return updated == 1


def finish_task(task_id, lease_token, result):
    now = timezone.now()
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(
            pk=task_id,
            lease_token=lease_token,
            status__in=[Task.Status.RUNNING, Task.Status.CANCEL_REQUESTED],
        ).first()
        if task is None:
            return False
        if task.status == Task.Status.CANCEL_REQUESTED:
            task.status = Task.Status.CANCELLED
            task.result = None
        else:
            task.status = Task.Status.SUCCEEDED
            task.result = result
        task.finished_at = now
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        task.save(
            update_fields=[
                "status",
                "result",
                "finished_at",
                "lease_owner",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return True


def fail_task(task_id, lease_token, error, retryable=True):
    now = timezone.now()
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(
            pk=task_id,
            lease_token=lease_token,
            status__in=[Task.Status.RUNNING, Task.Status.CANCEL_REQUESTED],
        ).first()
        if task is None:
            return False
        if task.status == Task.Status.CANCEL_REQUESTED:
            task.status = Task.Status.CANCELLED
            task.finished_at = now
        elif retryable and task.attempts < task.max_attempts:
            task.status = Task.Status.QUEUED
            task.available_at = now + timedelta(seconds=min(2**task.attempts, 300))
        else:
            task.status = Task.Status.FAILED
            task.finished_at = now
        task.error = error
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        task.save(
            update_fields=[
                "status",
                "available_at",
                "error",
                "finished_at",
                "lease_owner",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return True


def execute_task(task, worker_id):
    if task.task_type == "demo.echo":
        return {"echo": task.payload}
    if task.task_type == "demo.sleep":
        seconds = task.payload.get("seconds", 1)
        if not isinstance(seconds, int) or seconds < 0 or seconds > 30:
            raise PermanentTaskError("seconds must be an integer between 0 and 30")
        for _ in range(seconds):
            if not renew_lease(task.id, task.lease_token, worker_id):
                raise PermanentTaskError("Task cancellation requested or lease lost")
            sleep(1)
        return {"slept_seconds": seconds}
    raise PermanentTaskError(f"Unsupported task type: {task.task_type}")


def process_task(task, worker_id):
    try:
        result = execute_task(task, worker_id)
        finish_task(task.id, task.lease_token, result)
    except PermanentTaskError as exc:
        fail_task(
            task.id,
            task.lease_token,
            {
                "code": "TASK_VALIDATION_FAILED",
                "message": str(exc),
                "retryable": False,
            },
            retryable=False,
        )
    except Exception as exc:
        fail_task(
            task.id,
            task.lease_token,
            {
                "code": "TASK_EXECUTION_FAILED",
                "message": type(exc).__name__,
                "retryable": True,
            },
            retryable=True,
        )


def process_once(worker_id, lease_seconds=30):
    task = claim_next_task(worker_id, lease_seconds=lease_seconds)
    if task is None:
        return False
    process_task(task, worker_id)
    return True
