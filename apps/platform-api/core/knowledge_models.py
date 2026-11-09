import uuid

from django.conf import settings
from django.db import models

from core.models import ModelConfig, Project, Tenant


class KnowledgeBase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="knowledge_bases",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="knowledge_bases",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_knowledge_bases",
    )
    embedding_model = models.ForeignKey(
        ModelConfig,
        on_delete=models.PROTECT,
        related_name="knowledge_bases",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    chunk_size = models.PositiveSmallIntegerField(default=1200)
    chunk_overlap = models.PositiveSmallIntegerField(default=160)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug"],
                name="uniq_knowledge_base_slug_per_project",
            )
        ]


class KnowledgeDocument(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INGESTING = "ingesting", "Ingesting"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    knowledge_base = models.ForeignKey(
        KnowledgeBase,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_knowledge_documents",
    )
    title = models.CharField(max_length=240)
    content = models.TextField()
    metadata = models.JSONField(default=dict)
    checksum = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    chunk_count = models.PositiveIntegerField(default=0)
    error = models.JSONField(null=True, blank=True)
    ingested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["knowledge_base", "status", "created_at"],
                name="knowledge_doc_status_idx",
            )
        ]
