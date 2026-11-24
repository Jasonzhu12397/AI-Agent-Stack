import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
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

    class AuthMode(models.TextChoices):
        """Who pays for the calls this provider serves.

        ``tenant_secret`` is the platform-issued key billed against the tenant
        pool. ``user_account`` means the caller brings their own account for
        the upstream platform: the provider is only a catalogue entry, the
        credential belongs to a linked user, and calls draw on that user's
        free allowance instead of the tenant's purchased balance.
        """

        TENANT_SECRET = "tenant_secret", "Tenant credential"
        USER_ACCOUNT = "user_account", "Linked user account"
        #: One operator-held credential serves every member: after single
        #: sign-on the member selects these models and never sees a key.
        PLATFORM_POOL = "platform_pool", "Platform credential (no per-user key)"

    name = models.CharField(max_length=120)
    provider_type = models.CharField(max_length=32, choices=ProviderType.choices)
    auth_mode = models.CharField(
        max_length=32,
        choices=AuthMode.choices,
        default=AuthMode.TENANT_SECRET,
    )
    preset_key = models.CharField(max_length=32, blank=True, default="")
    base_url = models.URLField(max_length=500, blank=True)
    credential_ref = models.CharField(max_length=160, blank=True)
    config = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    deployment_region = models.CharField(max_length=32, blank=True, default="")
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


class ProviderAccountLink(models.Model):
    """A user's own account on an upstream model platform.

    Nothing secret lives here: the row records that the user proved ownership
    of the account, which credential reference their calls should resolve, and
    whether the link is still active. Revoking keeps the row so audit history
    survives, which is why the uniqueness constraint only covers active links.
    """

    class Status(models.TextChoices):
        LINKED = "linked", "Linked"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="provider_account_links",
    )
    provider = models.ForeignKey(
        ModelProvider,
        on_delete=models.CASCADE,
        related_name="account_links",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.LINKED)
    account_label = models.CharField(max_length=120, blank=True, default="")
    credential_ref = models.CharField(max_length=160, blank=True)
    linked_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-linked_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"],
                condition=Q(status="linked"),
                name="uniq_active_provider_account_link",
            )
        ]
        indexes = [models.Index(fields=["user", "status"])]


class ProviderAccountUsage(models.Model):
    """Free-allowance ledger for calls made with a linked user account.

    Separate from the tenant ledger on purpose: a free call must never move the
    tenant's purchased balance, and a purchased call must never be reported as
    free usage. ``idempotency_key`` mirrors the tenant ledger so retries settle
    the same row instead of double-counting.
    """

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        SETTLED = "settled", "Settled"
        RELEASED = "released", "Released"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="provider_account_usages",
    )
    provider = models.ForeignKey(
        ModelProvider,
        on_delete=models.CASCADE,
        related_name="account_usages",
    )
    idempotency_key = models.CharField(max_length=160)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RESERVED)
    reserved_tokens = models.BigIntegerField(default=0)
    consumed_tokens = models.BigIntegerField(default=0)
    expires_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider", "idempotency_key"],
                name="uniq_provider_account_usage_key",
            )
        ]
        indexes = [models.Index(fields=["user", "provider", "settled_at"])]


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
    execution_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AgentTeam(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_teams")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_teams")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_agent_teams",
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
                name="uniq_agent_team_slug_per_project",
            )
        ]


class AgentTeamVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        AgentTeam,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    definition_snapshot = models.JSONField()
    workflow_version = models.ForeignKey(
        "WorkflowVersion",
        on_delete=models.PROTECT,
        related_name="agent_team_versions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_agent_team_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "version"],
                name="uniq_agent_team_version_number",
            )
        ]


class AgentTeamRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_version = models.ForeignKey(
        AgentTeamVersion,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    workflow_run = models.OneToOneField(
        "WorkflowRun",
        on_delete=models.CASCADE,
        related_name="agent_team_run",
    )
    input_text = models.TextField()
    output_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class TenantTokenBudget(models.Model):
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="token_budget")
    daily_limit = models.PositiveBigIntegerField(default=100_000)
    monthly_limit = models.PositiveBigIntegerField(default=1_000_000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class TenantTokenUsage(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        SETTLED = "settled", "Settled"
        RELEASED = "released", "Released"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="token_usage")
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="token_usage",
        null=True,
        blank=True,
    )
    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="token_usage",
        null=True,
        blank=True,
    )
    workflow_run = models.ForeignKey(
        "WorkflowRun",
        on_delete=models.CASCADE,
        related_name="token_usage",
        null=True,
        blank=True,
    )
    scope_ref = models.CharField(max_length=160, null=True, blank=True)
    idempotency_key = models.CharField(max_length=160)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RESERVED)
    reserved_tokens = models.PositiveBigIntegerField(default=0)
    consumed_tokens = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="uniq_tenant_token_usage_idempotency",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(agent_run__isnull=False, workflow_run__isnull=True)
                    | models.Q(agent_run__isnull=True, workflow_run__isnull=False)
                    | models.Q(
                        scope_ref__isnull=False,
                        agent_run__isnull=True,
                        workflow_run__isnull=True,
                    )
                ),
                name="tenant_token_usage_exactly_one_run",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "status", "settled_at"],
                name="tenant_token_settled_idx",
            ),
            models.Index(
                fields=["tenant", "status", "expires_at"],
                name="tenant_token_reserved_idx",
            ),
        ]


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
    deadline_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class WorkflowStepRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
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
    status = models.CharField(max_length=24, choices=Status.choices)
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
        LOCAL_RUNNER = "local_runner", "Local runner"

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
    client_certificate_fingerprint = models.CharField(max_length=64, blank=True)
    client_certificate_bound_at = models.DateTimeField(null=True, blank=True)
    client_certificate_revoked_at = models.DateTimeField(null=True, blank=True)
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
            ),
            models.UniqueConstraint(
                fields=["client_certificate_fingerprint"],
                condition=~models.Q(client_certificate_fingerprint=""),
                name="uniq_agent_client_cert_fingerprint",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "project", "node_type"], name="agent_node_scope_idx"),
            models.Index(fields=["status", "last_seen_at"], name="agent_node_status_idx"),
        ]


class AgentMachine(models.Model):
    class IdentityStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        CANDIDATE = "candidate", "Candidate"
        VERIFIED = "verified", "Verified"
        CONFLICT = "conflict", "Conflict"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="machines")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="machines")
    agent_node = models.OneToOneField(
        AgentNode,
        on_delete=models.CASCADE,
        related_name="machine",
    )
    vendor = models.CharField(max_length=256, blank=True)
    product_name = models.CharField(max_length=256, blank=True)
    system_uuid = models.CharField(max_length=128, blank=True)
    identity_material = models.JSONField(default=dict)
    identity_fingerprint = models.CharField(max_length=64, blank=True)
    identity_status = models.CharField(
        max_length=16,
        choices=IdentityStatus.choices,
        default=IdentityStatus.UNVERIFIED,
    )
    identity_conflicts = models.JSONField(default=list)
    hardware_inventory = models.JSONField(default=dict)
    inventory_source = models.CharField(max_length=48, default="agent_report")
    inventory_revision = models.PositiveIntegerField(default=0)
    observed_at = models.DateTimeField(null=True, blank=True)
    bmc_credential_ref = models.CharField(max_length=160, blank=True)
    bmc_endpoint = models.URLField(max_length=500, blank=True)
    bmc_trust_bundle_ref = models.CharField(max_length=160, blank=True)
    bmc_tls_verify = models.BooleanField(default=True)
    bmc_configured_at = models.DateTimeField(null=True, blank=True)
    bmc_identity_evidence = models.JSONField(default=dict)
    bmh_name = models.CharField(max_length=253, blank=True)
    capi_machine_name = models.CharField(max_length=253, blank=True)
    kubernetes_node_name = models.CharField(max_length=253, blank=True)
    lifecycle_state = models.CharField(max_length=32, default="discovered")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["agent_node__name"]
        indexes = [
            models.Index(
                fields=["tenant", "project", "identity_status"],
                name="machine_identity_idx",
            ),
            models.Index(
                fields=["tenant", "project", "observed_at"],
                name="machine_observed_idx",
            ),
        ]


class FirmwareUpdateJob(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="firmware_update_jobs"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="firmware_update_jobs"
    )
    machine = models.ForeignKey(
        AgentMachine,
        on_delete=models.CASCADE,
        related_name="firmware_update_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_firmware_updates",
    )
    image_url = models.CharField(max_length=500)
    component = models.CharField(max_length=160, blank=True)
    apply_time = models.CharField(max_length=32, default="Immediate")
    targets = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.SUBMITTED,
    )
    task_monitor = models.CharField(max_length=500, blank=True)
    task_state = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "machine", "idempotency_key"],
                name="uniq_firmware_job_idempotency",
            )
        ]


class AgentCommand(models.Model):
    class Status(models.TextChoices):
        WAITING_APPROVAL = "waiting_approval", "Waiting approval"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

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


class InfrastructureNetworkProfile(models.Model):
    """Project-scoped infrastructure network plan used by Metal3 and agents."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="infrastructure_network_profiles",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="infrastructure_network_profiles",
    )
    profile = models.JSONField(default=dict)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="infrastructure_network_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "project"],
                name="uniq_infra_network_profile_project",
            )
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


class AgentPluginDefinition(models.Model):
    class Protocol(models.TextChoices):
        HTTP_JSON = "http_json", "HTTP JSON"
        MCP = "mcp", "Model Context Protocol"
        A2A = "a2a", "Agent-to-Agent"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI Compatible"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="agent_plugin_definitions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_agent_plugin_definitions",
    )
    plugin_key = models.SlugField(max_length=100)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    version = models.CharField(max_length=40)
    protocol = models.CharField(max_length=32, choices=Protocol.choices)
    endpoint_url = models.URLField(max_length=500)
    auth_required = models.BooleanField(default=True)
    capabilities = models.JSONField(default=list)
    manifest = models.JSONField(default=dict)
    input_schema = models.JSONField(default=dict)
    output_schema = models.JSONField(default=dict)
    timeout_seconds = models.PositiveSmallIntegerField(default=60)
    max_concurrency = models.PositiveSmallIntegerField(default=4)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "plugin_key", "version"],
                name="uniq_agent_plugin_key_version",
            )
        ]

    def save(self, *args, **kwargs):
        # When cache-relevant definition fields change, every bound installation
        # is bumped in bulk so stale process-local capability caches are rejected
        # by revision verification on the next resolve in any worker process.
        update_fields = kwargs.get("update_fields")
        relevant = {"version", "enabled", "manifest"}
        touched = relevant if update_fields is None else relevant.intersection(update_fields)
        previous = None
        if not self._state.adding and touched:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("version", "enabled", "manifest")
                .first()
            )
        super().save(*args, **kwargs)
        if previous is not None and touched:
            changed = False
            if "version" in touched and previous["version"] != self.version:
                changed = True
            if "enabled" in touched and bool(previous["enabled"]) != bool(self.enabled):
                changed = True
            if "manifest" in touched and previous["manifest"] != self.manifest:
                changed = True
            if changed:
                AgentPluginInstallation.objects.filter(plugin=self).update(
                    revision=models.F("revision") + 1
                )


class AgentPluginInstallation(models.Model):
    class Status(models.TextChoices):
        INSTALLED = "installed", "Installed"
        DISABLED = "disabled", "Disabled"
        UNINSTALLED = "uninstalled", "Uninstalled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="agent_plugin_installations",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="agent_plugin_installations",
    )
    plugin = models.ForeignKey(
        AgentPluginDefinition,
        on_delete=models.PROTECT,
        related_name="installations",
    )
    plugin_version = models.CharField(max_length=40)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.INSTALLED,
    )
    granted_capabilities = models.JSONField(default=list)
    credential_ref = models.CharField(max_length=200, blank=True)
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="agent_plugin_installations",
    )
    # Monotonically increasing per-installation generation. Every save that
    # mutates install state bumps it; capability caches key their entries on
    # this revision and reject entries whose revision no longer matches the row.
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["plugin__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "plugin"],
                name="uniq_agent_plugin_installation_per_project",
            )
        ]

    def save(self, *args, **kwargs):
        # Any mutation of an existing installation bumps the revision so cached
        # resolutions are invalidated across processes. The bump is recorded in
        # update_fields when the caller pins them, so partial saves still bump.
        if self._state.adding:
            if self.revision is None:
                self.revision = 1
            super().save(*args, **kwargs)
            return
        self.revision = (self.revision or 0) + 1
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = list(update_fields)
            if "revision" not in update_fields:
                update_fields.append("revision")
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)


class AgentCapabilityInvocation(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(
        AgentPluginInstallation,
        on_delete=models.CASCADE,
        related_name="capability_invocations",
    )
    capability = models.CharField(max_length=100)
    # Wide enough for every declared protocol token (openai_compatible is 17 chars).
    protocol = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_code = models.CharField(max_length=64, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["installation", "capability", "created_at"],
                name="agent_cap_invocation_idx",
            )
        ]


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
            ),
            models.UniqueConstraint(
                fields=["installation"],
                condition=models.Q(status="healthy"),
                name="uniq_healthy_plugin_connection",
            ),
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
        null=True,
        blank=True,
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


from core import billing_models, data_controls_models, settings_models  # noqa: E402,F401
from core.chat_models import (  # noqa: E402,F401
    AgentChatAttachment,  # noqa: E402,F401
    AgentChatMessage,
    AgentChatThread,
)
from core.knowledge_models import KnowledgeBase, KnowledgeDocument  # noqa: E402,F401
from core.registry_models import RegistryEvent, RegistryImage  # noqa: E402,F401
from core.scheduled_models import ScheduledChatJob  # noqa: E402,F401
