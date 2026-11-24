"""Central container image registry catalog.

The platform ships one internal registry per deployment. Every image an agent,
EPH bootstrap or target cluster runs must be registered here by immutable
digest, scanned, signed and promoted before it can be pulled; the catalog is
the single source of truth the agents read when they build pull references.
"""

import uuid

from django.conf import settings
from django.db import models

from core.models import Project, Tenant


class RegistryImage(models.Model):
    """One image digest as seen by the platform registry."""

    class Category(models.TextChoices):
        PLATFORM = "platform", "Platform"
        AGENT = "agent", "Agent"
        INFRASTRUCTURE = "infrastructure", "Infrastructure"
        KUBERNETES = "kubernetes", "Kubernetes"
        NETWORK = "network", "Network"
        OBSERVABILITY = "observability", "Observability"
        WORKLOAD = "workload", "Workload"

    class Status(models.TextChoices):
        BUILDING = "building", "Building"
        SCANNING = "scanning", "Scanning"
        READY = "ready", "Ready"
        BLOCKED = "blocked", "Blocked"
        RETIRED = "retired", "Retired"

    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    class Stage(models.TextChoices):
        DEV = "dev", "Dev"
        STAGING = "staging", "Staging"
        PRODUCTION = "production", "Production"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="registry_images")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="registry_images")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_registry_images",
    )
    # Repository path inside the internal registry, e.g. "ai-agent-stack/eph-agent".
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices)
    source_repository = models.CharField(max_length=200, blank=True)
    build_ref = models.CharField(max_length=120, blank=True)
    # Deployments only ever reference a digest; a mutable tag is rejected on write.
    digest = models.CharField(max_length=80)
    size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.BUILDING)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.DEV)
    scan_status = models.CharField(
        max_length=16, choices=ScanStatus.choices, default=ScanStatus.PENDING
    )
    scan_findings = models.PositiveIntegerField(default=0)
    sbom_ref = models.CharField(max_length=200, blank=True)
    signed = models.BooleanField(default=False)
    promoted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name", "digest"],
                name="uniq_registry_image_digest_per_project",
            )
        ]


class RegistryEvent(models.Model):
    """Append-only trail of everything that happened to an image."""

    class Action(models.TextChoices):
        REGISTERED = "registered", "Registered"
        SCANNED = "scanned", "Scanned"
        SIGNED = "signed", "Signed"
        PROMOTED = "promoted", "Promoted"
        PULLED = "pulled", "Pulled"
        RETIRED = "retired", "Retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    image = models.ForeignKey(
        RegistryImage, on_delete=models.CASCADE, related_name="events"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="registry_events",
        null=True,
        blank=True,
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
