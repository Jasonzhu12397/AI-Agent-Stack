"""Recurring Control Agent turns.

A scheduled job is a prompt plus a cadence: the platform opens (or reuses) a
thread for the project and runs one Control Agent turn on the schedule, so the
answer lands in the same transcript a member would read. Dispatch happens in
the worker, which owns execution time; the console only reads and edits the
schedule.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from core.chat_models import AgentChatThread
from core.models import ModelConfig, Project, Tenant


class ScheduledChatJob(models.Model):
    class Cadence(models.TextChoices):
        HOURLY = "hourly", "Hourly"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="scheduled_chat_jobs"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="scheduled_chat_jobs"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scheduled_chat_jobs",
    )
    name = models.CharField(max_length=160)
    prompt = models.TextField()
    thread = models.ForeignKey(
        AgentChatThread,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_jobs",
    )
    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_chat_jobs",
    )
    cadence = models.CharField(
        max_length=16, choices=Cadence.choices, default=Cadence.DAILY
    )
    #: Local hour the run should happen at, 0-23.
    hour = models.PositiveSmallIntegerField(default=9)
    #: Local weekday for weekly jobs, 0 = Monday.
    weekday = models.PositiveSmallIntegerField(default=0)
    execution_target = models.CharField(
        max_length=16,
        choices=[("cloud", "Cloud"), ("local", "Local")],
        default="cloud",
    )
    effort = models.CharField(
        max_length=16,
        choices=[("auto", "Auto"), ("low", "Low")],
        default="auto",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    next_run_at = models.DateTimeField()
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_run_at", "created_at"]
        indexes = [
            models.Index(fields=["status", "next_run_at"], name="sched_job_due_idx"),
            models.Index(fields=["tenant", "project"], name="sched_job_scope_idx"),
        ]

    def __str__(self) -> str:
        return self.name
