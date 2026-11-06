import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


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


class AgentNode(models.Model):
    class NodeType(models.TextChoices):
        EPH_NODE = "eph_node", "EPH node"
        TARGET_CLUSTER = "target_cluster", "Target cluster"

    class Status(models.TextChoices):
        OFFLINE = "offline", "Offline"
        ONLINE = "online", "Online"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_nodes")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_nodes")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="enrolled_agent_nodes",
    )
    name = models.CharField(max_length=120)
    node_type = models.CharField(max_length=24, choices=NodeType.choices)
    cluster_name = models.CharField(max_length=120, blank=True)
    agent_version = models.CharField(max_length=40, blank=True)
    capabilities = models.JSONField(default=list)
    labels = models.JSONField(default=dict)
    credential_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OFFLINE)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"],
                name="uniq_agent_node_name_per_project",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "project", "node_type"], name="agent_node_scope_idx"),
            models.Index(fields=["status", "last_seen_at"], name="agent_node_status_idx"),
        ]


class AgentCommand(models.Model):
    class Status(models.TextChoices):
        WAITING_APPROVAL = "waiting_approval", "Waiting approval"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_commands")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_commands")
    node = models.ForeignKey(AgentNode, on_delete=models.CASCADE, related_name="commands")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_agent_commands",
    )
    command_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=Status.choices)
    idempotency_key = models.CharField(max_length=128)
    plan_hash = models.CharField(max_length=64)
    approved_plan_hash = models.CharField(max_length=64, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_agent_commands",
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deployment = models.ForeignKey(
        "ClusterDeployment",
        on_delete=models.SET_NULL,
        related_name="commands",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_agent_command_idempotency",
            )
        ]
        indexes = [
            models.Index(
                fields=["node", "status", "created_at"],
                name="agent_command_queue_idx",
            ),
            models.Index(
                fields=["tenant", "project", "created_at"],
                name="agent_command_scope_idx",
            ),
        ]


class ClusterDeployment(models.Model):
    class Status(models.TextChoices):
        PREFLIGHT_QUEUED = "preflight_queued", "Preflight queued"
        PLANNING = "planning", "Planning"
        PLAN_READY = "plan_ready", "Plan ready"
        WAITING_APPROVAL = "waiting_approval", "Waiting approval"
        PROVISIONING = "provisioning", "Provisioning"
        CONTROL_PLANE_READY = "control_plane_ready", "Control plane ready"
        WORKERS_READY = "workers_ready", "Workers ready"
        TARGET_AGENT_PENDING = "target_agent_pending", "Target agent pending"
        POSTFLIGHT = "postflight", "Postflight"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="cluster_deployments",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="cluster_deployments",
    )
    eph_node = models.ForeignKey(
        AgentNode,
        on_delete=models.PROTECT,
        related_name="cluster_deployments",
    )
    target_node = models.ForeignKey(
        AgentNode,
        on_delete=models.PROTECT,
        related_name="target_cluster_deployments",
        null=True,
        blank=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cluster_deployments",
    )
    cluster_name = models.SlugField(max_length=63)
    kubernetes_version = models.CharField(max_length=32)
    spec = models.JSONField(default=dict)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PREFLIGHT_QUEUED,
    )
    idempotency_key = models.CharField(max_length=128)
    plan_hash = models.CharField(max_length=64, blank=True)
    preflight_result = models.JSONField(null=True, blank=True)
    plan_result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_cluster_deployment_idempotency",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "project", "status"],
                name="cluster_deploy_scope_idx",
            ),
            models.Index(
                fields=["eph_node", "status", "created_at"],
                name="cluster_deploy_node_idx",
            ),
        ]



class ClusterDeploymentCheckpoint(models.Model):
    deployment = models.OneToOneField(
        ClusterDeployment,
        on_delete=models.CASCADE,
        related_name="checkpoint",
    )
    agent_node = models.ForeignKey(
        AgentNode,
        on_delete=models.PROTECT,
        related_name="deployment_checkpoints",
    )
    generation = models.PositiveIntegerField(default=1)
    sequence = models.PositiveBigIntegerField(default=0)
    phase = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class PluginDefinition(models.Model):
    class ConnectorType(models.TextChoices):
        NATIVE = "native", "Native"
        MCP = "mcp", "MCP"
        HTTP = "http", "HTTP"
        OPENAPI = "openapi", "OpenAPI"
        CUSTOM = "custom", "Custom"

    class ReviewStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="plugin_definitions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_plugin_definitions",
    )
    plugin_key = models.SlugField(max_length=100)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    version = models.CharField(max_length=40)
    connector_type = models.CharField(max_length=16, choices=ConnectorType.choices)
    maintainer = models.CharField(max_length=120, blank=True)
    license = models.CharField(max_length=80, blank=True)
    source_url = models.URLField(max_length=500, blank=True)
    manifest = models.JSONField(default=dict)
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.DRAFT,
    )
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "plugin_key", "version"],
                name="uniq_plugin_key_version_per_tenant",
            )
        ]


class PluginTool(models.Model):
    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class IdempotencyMode(models.TextChoices):
        NONE = "none", "None"
        READ_ONLY = "read_only", "Read only"
        IDEMPOTENT = "idempotent", "Idempotent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin = models.ForeignKey(
        PluginDefinition,
        on_delete=models.CASCADE,
        related_name="tools",
    )
    name = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    input_schema = models.JSONField(default=dict)
    output_schema = models.JSONField(default=dict)
    required_permissions = models.JSONField(default=list)
    risk_level = models.CharField(
        max_length=16,
        choices=RiskLevel.choices,
        default=RiskLevel.LOW,
    )
    timeout_seconds = models.PositiveSmallIntegerField(default=30)
    retry_policy = models.JSONField(default=dict)
    idempotency_mode = models.CharField(
        max_length=16,
        choices=IdempotencyMode.choices,
        default=IdempotencyMode.NONE,
    )
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["plugin", "name"],
                name="uniq_plugin_tool_name",
            )
        ]


class PluginInstallation(models.Model):
    class Status(models.TextChoices):
        INSTALLED = "installed", "Installed"
        DISABLED = "disabled", "Disabled"
        UNINSTALLED = "uninstalled", "Uninstalled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="plugin_installations",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="plugin_installations",
    )
    plugin = models.ForeignKey(
        PluginDefinition,
        on_delete=models.PROTECT,
        related_name="installations",
    )
    plugin_version = models.CharField(max_length=40)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.INSTALLED,
    )
    granted_permissions = models.JSONField(default=list)
    config = models.JSONField(default=dict)
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="plugin_installations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["plugin__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "plugin"],
                name="uniq_plugin_installation_per_project",
            )
        ]


class PluginConnection(models.Model):
    class AuthType(models.TextChoices):
        NONE = "none", "None"
        CREDENTIAL_REF = "credential_ref", "Credential reference"
        OAUTH_REF = "oauth_ref", "OAuth reference"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        HEALTHY = "healthy", "Healthy"
        ERROR = "error", "Error"
        DISABLED = "disabled", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(
        PluginInstallation,
        on_delete=models.CASCADE,
        related_name="connections",
    )
    name = models.CharField(max_length=120)
    auth_type = models.CharField(
        max_length=20,
        choices=AuthType.choices,
        default=AuthType.NONE,
    )
    credential_ref = models.CharField(max_length=200, blank=True)
    config = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "name"],
                name="uniq_plugin_connection_name",
            )
        ]


class ToolApprovalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        CONSUMED = "consumed", "Consumed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="tool_approvals")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tool_approvals")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tool_approvals")
    tool = models.ForeignKey(PluginTool, on_delete=models.PROTECT, related_name="approval_requests")
    installation = models.ForeignKey(
        PluginInstallation,
        on_delete=models.PROTECT,
        related_name="approval_requests",
    )
    connection = models.ForeignKey(
        PluginConnection,
        on_delete=models.PROTECT,
        related_name="approval_requests",
        null=True,
        blank=True,
    )
    call_id = models.CharField(max_length=128)
    arguments = models.JSONField()
    arguments_hash = models.CharField(max_length=64)
    plugin_version = models.CharField(max_length=40)
    execution_checkpoint = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_tool_requests",
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="rejected_tool_requests",
        null=True,
        blank=True,
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "call_id"],
                name="uniq_tool_approval_task_call",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "project", "status", "expires_at"],
                name="tool_approval_scope_idx",
            ),
        ]



class AuditEvent(models.Model):
    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        DENIED = "denied", "Denied"
        FAILURE = "failure", "Failure"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=120)
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.SUCCESS,
    )
    request_id = models.CharField(max_length=64, blank=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)
    metadata = models.JSONField(default=dict)
    previous_hash = models.CharField(max_length=64, blank=True)
    event_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["tenant", "-created_at"],
                name="audit_tenant_time_idx",
            ),
            models.Index(
                fields=["tenant", "event_type", "-created_at"],
                name="audit_tenant_type_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Audit events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events cannot be deleted through the model API.")


from core.knowledge_models import KnowledgeBase, KnowledgeDocument  # noqa: E402,F401
