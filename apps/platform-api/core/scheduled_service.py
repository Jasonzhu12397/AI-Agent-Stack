"""Schedule arithmetic and dispatch for recurring Control Agent turns.

The worker owns time: on every poll it asks this module for due jobs, claims one
by moving ``next_run_at`` forward inside a transaction, and then runs the turn
exactly as the console would. Claiming before running is what keeps two workers
from answering the same schedule slot twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from core import chat_runtime
from core.chat_models import AgentChatMessage, AgentChatThread
from core.scheduled_models import ScheduledChatJob


def next_occurrence(job, after: datetime) -> datetime:
    """The first slot strictly after ``after`` that matches the job's cadence.

    Slots are daily/weekly at ``hour`` or hourly at minute zero, all in the
    server's current timezone so a schedule reads the same as a clock.
    """
    local = timezone.localtime(after)
    candidate = local.replace(minute=0, second=0, microsecond=0)
    if job.cadence == ScheduledChatJob.Cadence.HOURLY:
        candidate = candidate + timedelta(hours=1)
        if candidate <= local:
            candidate = candidate + timedelta(hours=1)
        return candidate
    candidate = candidate.replace(hour=int(job.hour))
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    if job.cadence == ScheduledChatJob.Cadence.WEEKLY:
        target_weekday = int(job.weekday)
        while candidate.weekday() != target_weekday:
            candidate = candidate + timedelta(days=1)
    return candidate


def _default_model_config(tenant):
    from django.db.models import Q

    from core.models import ModelConfig

    return (
        ModelConfig.objects.select_related("provider")
        .filter(
            Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
            provider__enabled=True,
            enabled=True,
        )
        .order_by("provider__provider_type", "provider__name", "model_name")
        .first()
    )


def _ensure_thread(job):
    if job.thread_id is not None:
        return job.thread
    thread = AgentChatThread.objects.create(
        tenant=job.tenant,
        project=job.project,
        created_by=job.created_by,
        title=job.name[:200],
        model_config=job.model_config or _default_model_config(job.tenant),
        execution_target=job.execution_target,
        effort=job.effort,
    )
    job.thread = thread
    job.save(update_fields=["thread", "updated_at"])
    return thread


def run_job(job, *, trigger: str = "schedule") -> dict:
    """Run one scheduled turn and persist it in the job's thread.

    Returns ``{"thread_id", "user_message_id", "assistant_message_id",
    "error_code"}``; an execution failure is stored as a message error, not
    raised, because a schedule must survive a bad provider day.
    """
    thread = _ensure_thread(job)
    thread.execution_target = job.execution_target
    thread.effort = job.effort
    if job.model_config_id and thread.model_config_id != job.model_config_id:
        thread.model_config = job.model_config
    with transaction.atomic():
        user_message = AgentChatMessage.objects.create(
            thread=thread,
            role=AgentChatMessage.Role.USER,
            content=job.prompt,
            author=job.created_by,
            context={"trigger": trigger, "scheduled_job_id": str(job.id)},
        )
        thread.save(update_fields=["updated_at", "execution_target", "effort", "model_config"])

    model_config = thread.model_config or _default_model_config(job.tenant)
    error_code = ""
    content = ""
    usage: dict = {}
    if model_config is None:
        error_code = "MODEL_NOT_CONFIGURED"
    else:
        messages = [
            {"role": "system", "content": chat_runtime.system_prompt(thread, {})},
            {"role": "user", "content": job.prompt},
        ]
        try:
            content, usage = chat_runtime.generate(
                job.tenant,
                model_config,
                messages,
                user=job.created_by,
                idempotency_key=f"scheduled:{job.id}:{user_message.id}",
                effort=job.effort,
                execution_target=job.execution_target,
            )
        except chat_runtime.ChatRuntimeError as exc:
            content, usage, error_code = exc.message, {}, exc.code
    with transaction.atomic():
        assistant = AgentChatMessage.objects.create(
            thread=thread,
            role=AgentChatMessage.Role.ASSISTANT,
            content=content,
            usage=usage,
            error_code=error_code,
        )
        assistant_id = str(assistant.id)
        job.last_run_at = timezone.now()
        job.last_error_code = error_code
        job.next_run_at = next_occurrence(job, job.last_run_at)
        job.save(update_fields=["last_run_at", "last_error_code", "next_run_at", "updated_at"])
    return {
        "thread_id": str(thread.id),
        "user_message_id": str(user_message.id),
        "assistant_message_id": assistant_id,
        "error_code": error_code,
    }


def claim_due_jobs(now: datetime | None = None, limit: int = 5) -> list[ScheduledChatJob]:
    """Move due jobs forward and return them, so only one worker runs each slot."""
    now = now or timezone.now()
    claimed = []
    due = list(
        ScheduledChatJob.objects.filter(
            status=ScheduledChatJob.Status.ACTIVE, next_run_at__lte=now
        ).order_by("next_run_at")[:limit]
    )
    for job in due:
        with transaction.atomic():
            updated = ScheduledChatJob.objects.filter(
                pk=job.id,
                status=ScheduledChatJob.Status.ACTIVE,
                next_run_at=job.next_run_at,
            ).update(next_run_at=next_occurrence(job, now), updated_at=now)
            if updated == 1:
                claimed.append(job)
    return claimed


def dispatch_due_jobs(now: datetime | None = None, limit: int = 5) -> int:
    """Run every due job once. Returns how many turns were executed."""
    ran = 0
    for job in claim_due_jobs(now=now, limit=limit):
        try:
            run_job(job, trigger="schedule")
        except Exception as exc:  # a schedule must not kill the worker loop
            ScheduledChatJob.objects.filter(pk=job.id).update(
                last_error_code=type(exc).__name__[:64], updated_at=timezone.now()
            )
        ran += 1
    return ran
