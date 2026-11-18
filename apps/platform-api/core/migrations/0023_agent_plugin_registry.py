import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_one_healthy_plugin_connection"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentPluginDefinition",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("plugin_key", models.SlugField(max_length=100)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("version", models.CharField(max_length=40)),
                ("protocol", models.CharField(choices=[("http_json", "HTTP JSON"), ("mcp", "Model Context Protocol"), ("a2a", "Agent-to-Agent")], max_length=16)),
                ("endpoint_url", models.URLField(max_length=500)),
                ("auth_required", models.BooleanField(default=True)),
                ("capabilities", models.JSONField(default=list)),
                ("input_schema", models.JSONField(default=dict)),
                ("output_schema", models.JSONField(default=dict)),
                ("timeout_seconds", models.PositiveSmallIntegerField(default=60)),
                ("max_concurrency", models.PositiveSmallIntegerField(default=4)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_agent_plugin_definitions", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_plugin_definitions", to="core.tenant")),
            ],
            options={
                "ordering": ["name", "-created_at"],
                "constraints": [
                    models.UniqueConstraint(fields=("tenant", "plugin_key", "version"), name="uniq_agent_plugin_key_version"),
                ],
            },
        ),
        migrations.CreateModel(
            name="AgentPluginInstallation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("plugin_version", models.CharField(max_length=40)),
                ("status", models.CharField(choices=[("installed", "Installed"), ("disabled", "Disabled"), ("uninstalled", "Uninstalled")], default="installed", max_length=16)),
                ("granted_capabilities", models.JSONField(default=list)),
                ("credential_ref", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("installed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="agent_plugin_installations", to=settings.AUTH_USER_MODEL)),
                ("plugin", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="installations", to="core.agentplugindefinition")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_plugin_installations", to="core.project")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_plugin_installations", to="core.tenant")),
            ],
            options={
                "ordering": ["plugin__name"],
                "constraints": [
                    models.UniqueConstraint(fields=("project", "plugin"), name="uniq_agent_plugin_installation_per_project"),
                ],
            },
        ),
    ]
