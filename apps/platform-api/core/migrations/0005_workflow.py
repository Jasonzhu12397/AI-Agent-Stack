import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_agent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkflowDefinition",
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
                ("slug", models.SlugField(max_length=80)),
                ("description", models.TextField(blank=True)),
                ("draft_definition", models.JSONField(default=dict)),
                ("current_version", models.PositiveIntegerField(default=0)),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_workflows",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflows",
                        to="core.project",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflows",
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("project", "slug"),
                        name="uniq_workflow_slug_per_project",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WorkflowVersion",
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
                ("version", models.PositiveIntegerField()),
                ("definition_snapshot", models.JSONField()),
                ("published_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="published_workflow_versions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "workflow",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="core.workflowdefinition",
                    ),
                ),
            ],
            options={
                "ordering": ["-version"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("workflow", "version"),
                        name="uniq_workflow_version_number",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WorkflowRun",
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
                ("input_data", models.JSONField(default=dict)),
                ("state", models.JSONField(default=dict)),
                ("output_data", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "task",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workflow_run",
                        to="core.task",
                    ),
                ),
                (
                    "workflow_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="runs",
                        to="core.workflowversion",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="WorkflowStepRun",
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
                ("node_id", models.CharField(max_length=120)),
                ("node_type", models.CharField(max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        max_length=16,
                    ),
                ),
                ("input_data", models.JSONField(default=dict)),
                ("output_data", models.JSONField(blank=True, null=True)),
                ("error", models.JSONField(blank=True, null=True)),
                ("attempt", models.PositiveSmallIntegerField(default=1)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "workflow_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="core.workflowrun",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("workflow_run", "node_id"),
                        name="uniq_workflow_step_run_node",
                    )
                ],
            },
        ),
    ]
