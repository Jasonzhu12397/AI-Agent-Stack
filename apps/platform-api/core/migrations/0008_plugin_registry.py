import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_cluster_deployment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PluginDefinition",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("plugin_key", models.SlugField(max_length=100)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("version", models.CharField(max_length=40)),
                (
                    "connector_type",
                    models.CharField(
                        choices=[
                            ("native", "Native"),
                            ("mcp", "MCP"),
                            ("http", "HTTP"),
                            ("openapi", "OpenAPI"),
                            ("custom", "Custom"),
                        ],
                        max_length=16,
                    ),
                ),
                ("maintainer", models.CharField(blank=True, max_length=120)),
                ("license", models.CharField(blank=True, max_length=80)),
                ("source_url", models.URLField(blank=True, max_length=500)),
                ("manifest", models.JSONField(default=dict)),
                (
                    "review_status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_plugin_definitions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plugin_definitions",
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["name", "-created_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "plugin_key", "version"),
                        name="uniq_plugin_key_version_per_tenant",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PluginTool",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.SlugField(max_length=100)),
                ("description", models.TextField(blank=True)),
                ("input_schema", models.JSONField(default=dict)),
                ("output_schema", models.JSONField(default=dict)),
                ("required_permissions", models.JSONField(default=list)),
                (
                    "risk_level",
                    models.CharField(
                        choices=[
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("critical", "Critical"),
                        ],
                        default="low",
                        max_length=16,
                    ),
                ),
                ("timeout_seconds", models.PositiveSmallIntegerField(default=30)),
                ("retry_policy", models.JSONField(default=dict)),
                (
                    "idempotency_mode",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("read_only", "Read only"),
                            ("idempotent", "Idempotent"),
                        ],
                        default="none",
                        max_length=16,
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                (
                    "plugin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tools",
                        to="core.plugindefinition",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("plugin", "name"),
                        name="uniq_plugin_tool_name",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PluginInstallation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("plugin_version", models.CharField(max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("installed", "Installed"),
                            ("disabled", "Disabled"),
                            ("uninstalled", "Uninstalled"),
                        ],
                        default="installed",
                        max_length=16,
                    ),
                ),
                ("granted_permissions", models.JSONField(default=list)),
                ("config", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "installed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="plugin_installations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "plugin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installations",
                        to="core.plugindefinition",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plugin_installations",
                        to="core.project",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plugin_installations",
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["plugin__name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("project", "plugin"),
                        name="uniq_plugin_installation_per_project",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PluginConnection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "auth_type",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("credential_ref", "Credential reference"),
                            ("oauth_ref", "OAuth reference"),
                        ],
                        default="none",
                        max_length=20,
                    ),
                ),
                ("credential_ref", models.CharField(blank=True, max_length=200)),
                ("config", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("healthy", "Healthy"),
                            ("error", "Error"),
                            ("disabled", "Disabled"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("last_health_check_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="connections",
                        to="core.plugininstallation",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("installation", "name"),
                        name="uniq_plugin_connection_name",
                    ),
                ],
            },
        ),
    ]
