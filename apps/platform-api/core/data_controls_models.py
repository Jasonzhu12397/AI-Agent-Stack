import uuid

from django.conf import settings
from django.db import models

from core.models import Project, Tenant


class DataExportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="data_export_jobs",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="data_export_jobs",
        null=True,
        blank=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="data_export_jobs",
    )
    scope = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    idempotency_key = models.CharField(max_length=128)
    row_count = models.PositiveIntegerField(default=0)
    file_ref = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_data_export_idempotency",
            )
        ]


class DeletionRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        EXECUTED = "executed", "Executed"
        REJECTED = "rejected", "Rejected"

    class Scope(models.TextChoices):
        ARCHIVED_CONTENT = "archived_content", "Archived content"
        EXPORTS = "exports", "Exports"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="deletion_requests",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="deletion_requests",
        null=True,
        blank=True,
    )
    scope = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.REQUESTED,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="deletion_requests",
    )
    idempotency_key = models.CharField(max_length=128)
    executed_at = models.DateTimeField(null=True, blank=True)
    audit_ref = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_deletion_request_idempotency",
            )
        ]


class ArchiveAction(models.Model):
    class Action(models.TextChoices):
        RESTORE = "restore", "Restore"
        PURGE = "purge", "Purge"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="archive_actions",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="archive_actions",
    )
    item_type = models.CharField(max_length=40)
    item_id = models.CharField(max_length=64)
    action = models.CharField(max_length=10, choices=Action.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="archive_actions",
    )
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "item_type", "item_id", "action"],
                name="uniq_archive_action_per_object_action",
            )
        ]
