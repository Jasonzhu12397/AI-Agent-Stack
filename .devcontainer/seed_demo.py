"""Seed a demo workspace so the console opens with data in it.

Django settings must exist before the models are imported, which is why the
imports below follow ``django.setup()`` instead of sitting at the top.
"""

# ruff: noqa: E402

import os
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_api.settings")

import django

django.setup()

from core.billing_models import Plan, Subscription
from core.models import (
    AgentCapabilityInvocation,
    AgentCommand,
    AgentDefinition,
    AgentNode,
    AgentPluginDefinition,
    AgentPluginInstallation,
    AgentRun,
    AgentVersion,
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    TenantTokenBudget,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowVersion,
)
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

user, _ = User.objects.get_or_create(
    username="demo",
    defaults={"email": "demo@acme.ai", "is_staff": True},
)
user.set_password("demo1234")
user.save()

tenant, _ = Tenant.objects.get_or_create(slug="acme", defaults={"name": "Acme AI"})
TenantMembership.objects.get_or_create(
    tenant=tenant, user=user, defaults={"role": TenantMembership.Role.OWNER}
)

project, _ = Project.objects.get_or_create(
    tenant=tenant, slug="prod-cluster", defaults={"name": "Prod Cluster"}
)
ProjectMembership.objects.get_or_create(
    project=project, user=user, defaults={"role": ProjectMembership.Role.ADMIN}
)

plans = [
    {
        "code": "starter",
        "name": "Starter",
        "description": "For a single team getting started with agent runs.",
        "price_cents": 9900,
        "token_quota_daily": 200_000,
        "token_quota_monthly": 4_000_000,
        "token_quota_daily_expired": 20_000,
        "token_quota_monthly_expired": 400_000,
        "trial_days": 14,
        "features": ["3 projects", "Community support"],
    },
    {
        "code": "pro",
        "name": "Pro",
        "description": "Production agents with token budget guards and audit.",
        "price_cents": 39900,
        "token_quota_daily": 1_000_000,
        "token_quota_monthly": 20_000_000,
        "token_quota_daily_expired": 50_000,
        "token_quota_monthly_expired": 1_000_000,
        "trial_days": 14,
        "features": ["Unlimited projects", "Audit retention", "Priority support"],
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Dedicated capacity, bare-metal delivery and SLA.",
        "price_cents": 199900,
        "token_quota_daily": 5_000_000,
        "token_quota_monthly": 120_000_000,
        "token_quota_daily_expired": 100_000,
        "token_quota_monthly_expired": 2_000_000,
        "trial_days": 0,
        "features": ["Dedicated runners", "SSO", "99.9% SLA"],
    },
]
for payload in plans:
    Plan.objects.update_or_create(code=payload["code"], defaults=payload)

pro = Plan.objects.get(code="pro")
now = timezone.now()
Subscription.objects.update_or_create(
    tenant=tenant,
    plan=pro,
    defaults={
        "status": Subscription.Status.ACTIVE,
        "started_at": now - timedelta(days=40),
        "current_period_start": now - timedelta(days=10),
        "current_period_end": now + timedelta(days=20),
        "renewal_mode": Subscription.RenewalMode.MANUAL,
    },
)

TenantTokenBudget.objects.update_or_create(
    tenant=tenant, defaults={"daily_limit": 1_000_000, "monthly_limit": 20_000_000}
)

providers = [
    (
        "DeepSeek V4.1 (domestic)",
        "openai_compatible",
        "https://api.deepseek.example/v1",
        "domestic",
    ),
    ("Qwen3.8 Max (domestic)", "openai_compatible", "https://dashscope.example/v1", "domestic"),
    ("Local echo", "fake", "", "domestic"),
    (
        "GPT-5.6 (overseas, unroutable)",
        "openai_compatible",
        "https://api.openai.example/v1",
        "overseas",
    ),
]
for name, kind, base, region in providers:
    ModelProvider.objects.update_or_create(
        tenant=tenant,
        name=name,
        defaults={
            "created_by": user,
            "provider_type": kind,
            "base_url": base,
            "deployment_region": region,
            "enabled": True,
            "credential_ref": "env://PLUGIN_SECRET_MODEL_KEY" if base else "",
            "config": {"default_model": "demo-model"},
        },
    )


def manifest(base, operations, capabilities):
    return {
        "http": {"base_url": base, "operations": operations},
        "capabilities": capabilities,
    }


def capability(name, description):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "risk_level": "low",
        "timeout_seconds": 30,
    }


plugins = [
    (
        "github-agent",
        "GitHub Agent",
        "Read issues and open pull requests inside the bound repository.",
        "http_json",
        "https://github-agent.example.com",
        True,
        [
            capability("issue.read", "Read an issue by number"),
            capability("pr.open", "Open a pull request"),
        ],
    ),
    (
        "metrics-agent",
        "Metrics Agent",
        "Query the cluster metrics warehouse without credentials.",
        "openai_compatible",
        "https://metrics-agent.example.com/v1",
        False,
        [capability("metrics.query", "Run a PromQL query")],
    ),
    (
        "vault-agent",
        "Vault Agent",
        "Rotate and read secrets through the internal vault.",
        "mcp",
        "https://vault-agent.example.com/mcp",
        True,
        [capability("secret.read", "Read one secret document")],
    ),
    (
        "triage-agent",
        "Triage Agent",
        "Delegates to an external A2A triage service.",
        "a2a",
        "https://triage-agent.example.com",
        False,
        [capability("triage.classify", "Classify an alert")],
    ),
]

installations = []
for key, name, description, protocol, base, auth, capabilities in plugins:
    caps = [item["name"] for item in capabilities]
    definition, _ = AgentPluginDefinition.objects.update_or_create(
        tenant=tenant,
        plugin_key=key,
        defaults={
            "created_by": user,
            "name": name,
            "description": description,
            "version": "1.4.2",
            "protocol": protocol,
            "endpoint_url": base + "/invoke",
            "auth_required": auth,
            "capabilities": capabilities,
            "manifest": manifest(
                base,
                {cap: {"method": "GET", "path": "/v1/" + cap} for cap in caps},
                capabilities,
            ),
            "enabled": True,
            "timeout_seconds": 30,
        },
    )
    installations.append((definition, caps))

AgentPluginInstallation.objects.filter(project=project).delete()
installed = {}
for definition, caps in installations:
    if definition.plugin_key == "triage-agent":
        continue
    row, _ = AgentPluginInstallation.objects.update_or_create(
        project=project,
        plugin=definition,
        defaults={
            "tenant": tenant,
            "plugin_version": definition.version,
            "status": AgentPluginInstallation.Status.INSTALLED,
            "granted_capabilities": caps,
            "installed_by": user,
            "credential_ref": (
                "vault://merchant/github-agent#token" if definition.auth_required else ""
            ),
        },
    )
    installed[definition.plugin_key] = row

AgentCapabilityInvocation.objects.filter(installation__project=project).delete()
records = [
    ("github-agent", "issue.read", "success", "", 412),
    ("github-agent", "pr.open", "success", "", 980),
    ("github-agent", "issue.read", "failed", "CAPABILITY_INVOCATION_FAILED", 2200),
    ("metrics-agent", "metrics.query", "success", "", 180),
    ("metrics-agent", "metrics.query", "success", "", 240),
    ("vault-agent", "secret.read", "rejected", "CREDENTIAL_NOT_FOUND", 0),
]
for index, (key, cap, status, code, latency) in enumerate(records):
    row = installed[key]
    AgentCapabilityInvocation.objects.create(
        installation=row,
        capability=cap,
        protocol=row.plugin.protocol,
        status=status,
        error_code=code,
        latency_ms=latency,
        created_at=now - timedelta(minutes=index * 17 + 3),
    )

Task.objects.filter(project=project).delete()
for index, (kind, status) in enumerate(
    [
        ("agent.run", Task.Status.SUCCEEDED),
        ("agent.team.run", Task.Status.SUCCEEDED),
        ("cluster.provision", Task.Status.RUNNING),
        ("cluster.backup", Task.Status.WAITING_APPROVAL),
        ("workflow.run", Task.Status.FAILED),
    ]
):
    Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type=kind,
        payload={"demo": True},
        status=status,
        idempotency_key=f"demo-{index}",
        available_at=now,
        created_at=now - timedelta(minutes=index * 12),
    )


# --- Models the console can actually talk to -------------------------------
# Every provider gets at least one enabled model so the chat and the model
# pickers are usable straight after bootstrap. The local echo provider is
# marked as the recommended default: it needs no credential and always answers.
for name, kind, _base, _region in providers:
    provider = ModelProvider.objects.get(tenant=tenant, name=name)
    ModelConfig.objects.update_or_create(
        provider=provider,
        model_name="demo-echo" if kind == "fake" else "demo-model",
        defaults={
            "display_name": "Local echo" if kind == "fake" else f"{name} default",
            "capabilities": ["chat"],
            "context_window": 32768,
            "max_output_tokens": 1024,
            "enabled": True,
        },
    )

# --- M1: a published agent plus a run that actually succeeded ---------------
agent, _ = AgentDefinition.objects.update_or_create(
    tenant=tenant,
    project=project,
    slug="demo-support-agent",
    defaults={
        "created_by": user,
        "name": "Demo Support Agent",
        "description": "Answers questions about the demo fleet.",
        "draft_config": {
            "model_config_id": str(ModelConfig.objects.filter(provider__tenant=tenant).first().id)
        },
        "current_version": 1,
    },
)
agent_version, _ = AgentVersion.objects.update_or_create(
    agent=agent,
    version=1,
    defaults={
        "created_by": user,
        "config_snapshot": agent.draft_config,
    },
)
agent_task, _ = Task.objects.update_or_create(
    tenant=tenant,
    idempotency_key="demo-agent-run",
    defaults={
        "project": project,
        "created_by": user,
        "task_type": "agent.run",
        "payload": {"demo": True},
        "status": Task.Status.SUCCEEDED,
        "available_at": now,
        "finished_at": now,
        "attempts": 1,
    },
)
AgentRun.objects.update_or_create(
    task=agent_task,
    defaults={
        "agent_version": agent_version,
        "input_text": "Which machines are waiting for enrollment?",
        "output_text": "Three machines are queued; two already have BMC credentials.",
        "input_tokens": 96,
        "output_tokens": 120,
        "provider_name": "Local echo",
        "model_name": "demo-echo",
        "latency_ms": 240,
    },
)

# --- M2: a published workflow whose steps were persisted and completed ------
workflow, _ = WorkflowDefinition.objects.update_or_create(
    project=project,
    slug="demo-fleet-check",
    defaults={
        "tenant": tenant,
        "created_by": user,
        "name": "Demo Fleet Check",
        "description": "Collect inventory, ask the model to summarise it.",
        "current_version": 1,
        "draft_definition": {
            "nodes": [
                {"id": "collect", "type": "llm", "prompt": "List the fleet."},
                {"id": "summarise", "type": "llm", "prompt": "Summarise."},
            ],
            "edges": [{"from": "collect", "to": "summarise"}],
        },
    },
)
workflow_version, _ = WorkflowVersion.objects.update_or_create(
    workflow=workflow,
    version=1,
    defaults={"created_by": user, "definition_snapshot": workflow.draft_definition},
)
workflow_task, _ = Task.objects.update_or_create(
    tenant=tenant,
    idempotency_key="demo-workflow-run",
    defaults={
        "project": project,
        "created_by": user,
        "task_type": "workflow.run",
        "payload": {"demo": True},
        "status": Task.Status.SUCCEEDED,
        "available_at": now,
        "finished_at": now,
        "attempts": 1,
    },
)
workflow_run, _ = WorkflowRun.objects.update_or_create(
    task=workflow_task,
    defaults={
        "workflow_version": workflow_version,
        "input_data": {"demo": True},
        "state": {"current": None},
        "output_data": {"summary": "Two machines ready."},
        "finished_at": now,
    },
)
for node_id in ("collect", "summarise"):
    WorkflowStepRun.objects.update_or_create(
        workflow_run=workflow_run,
        node_id=node_id,
        defaults={
            "node_type": "llm",
            "status": WorkflowStepRun.Status.SUCCEEDED,
            "output_data": {"text": f"{node_id} done"},
            "started_at": now,
            "finished_at": now,
        },
    )

# --- M3: an enrolled node plus a command that passed the approval gate ------
node, _ = AgentNode.objects.update_or_create(
    project=project,
    name="demo-eph-node",
    defaults={
        "tenant": tenant,
        "created_by": user,
        "node_type": AgentNode.NodeType.EPH_NODE,
        "cluster_name": "prod-cluster",
        "agent_version": "1.4.2",
        "capabilities": ["inventory.report", "pxe.boot"],
        "labels": {"pool": "pool4"},
        "credential_hash": "0" * 64,
        "status": AgentNode.Status.ONLINE,
        "last_seen_at": now,
        "last_seen_ip": "10.0.4.11",
    },
)
AgentCommand.objects.update_or_create(
    node=node,
    idempotency_key="demo-inventory-command",
    defaults={
        "tenant": tenant,
        "project": project,
        "requested_by": user,
        "command_type": "inventory.report",
        "payload": {"refresh": True},
        "status": AgentCommand.Status.SUCCEEDED,
        "plan_hash": "0" * 64,
        "approved_plan_hash": "0" * 64,
        "approved_by": user,
        "approved_at": now,
        "attempts": 1,
        "result": {"machines": 4},
        "started_at": now,
        "finished_at": now,
    },
)

print("seeded", tenant.slug, project.slug)
