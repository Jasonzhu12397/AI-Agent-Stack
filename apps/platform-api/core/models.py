import uuid

from django.conf import settings
from django.db import models


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TenantMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="uniq_tenant_user_membership")
        ]


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="uniq_project_slug_per_tenant")
        ]

    def __str__(self) -> str:
        return f"{self.tenant.slug}/{self.slug}"


class ProjectMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "user"], name="uniq_project_user_membership")
        ]


class Task(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        WAITING_APPROVAL = "waiting_approval", "Waiting approval"
        CANCEL_REQUESTED = "cancel_requested", "Cancel requested"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tasks")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_agent_tasks",
    )
    task_type = models.CharField(max_length=120)
    payload = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    idempotency_key = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    available_at = models.DateTimeField()
    lease_owner = models.CharField(max_length=120, null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_task_idempotency_per_tenant",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "available_at", "created_at"],
                name="task_queue_idx",
            ),
            models.Index(
                fields=["tenant", "project", "created_at"],
                name="task_scope_idx",
            ),
        ]


class ModelProvider(models.Model):
    class ProviderType(models.TextChoices):
        FAKE = "fake", "Fake model"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI compatible"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="model_providers",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_model_providers",
    )
    name = models.CharField(max_length=120)
    provider_type = models.CharField(max_length=32, choices=ProviderType.choices)
    base_url = models.URLField(max_length=500, blank=True)
    credential_ref = models.CharField(max_length=160, blank=True)
    config = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="uniq_model_provider_name_per_tenant",
            )
        ]


class ModelConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(
        ModelProvider,
        on_delete=models.CASCADE,
        related_name="models",
    )
    model_name = models.CharField(max_length=160)
    display_name = models.CharField(max_length=160)
    capabilities = models.JSONField(default=list)
    context_window = models.PositiveIntegerField(default=32768)
    max_output_tokens = models.PositiveIntegerField(default=4096)
    input_cost_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=0,
    )
    output_cost_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=0,
    )
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["provider__name", "display_name", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "model_name", "version"],
                name="uniq_model_config_version",
            )
        ]


class AgentDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agents")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agents")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_agents",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    draft_config = models.JSONField(default=dict)
    current_version = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug"],
                name="uniq_agent_slug_per_project",
            )
        ]


class AgentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        AgentDefinition,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    config_snapshot = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_agent_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent", "version"],
                name="uniq_agent_version_number",
            )
        ]


class AgentRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.OneToOneField(
        Task,
        on_delete=models.CASCADE,
        related_name="agent_run",
    )
    agent_version = models.ForeignKey(
        AgentVersion,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    input_text = models.TextField()
    output_text = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    provider_name = models.CharField(max_length=120, blank=True)
    model_name = models.CharField(max_length=160, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class WorkflowDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workflows")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="workflows")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_workflows",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    draft_definition = models.JSONField(default=dict)
    current_version = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "slug"],
                name="uniq_workflow_slug_per_project",
            )
        ]


class WorkflowVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(
        WorkflowDefinition,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    definition_snapshot = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_workflow_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version"],
                name="uniq_workflow_version_number",
            )
        ]


class WorkflowRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.OneToOneField(
        Task,
        on_delete=models.CASCADE,
        related_name="workflow_run",
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    input_data = models.JSONField(default=dict)
    state = models.JSONField(default=dict)
    output_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class WorkflowStepRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    node_id = models.CharField(max_length=120)
    node_type = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices)
    input_data = models.JSONField(default=dict)
    output_data = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    attempt = models.PositiveSmallIntegerField(default=1)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "node_id"],
                name="uniq_workflow_step_run_node",
            )
        ]
