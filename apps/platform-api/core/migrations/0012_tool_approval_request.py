import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_audit_event"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ToolApprovalRequest",
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
                ("call_id", models.CharField(max_length=128)),
                ("arguments", models.JSONField()),
                ("arguments_hash", models.CharField(max_length=64)),
                ("plugin_version", models.CharField(max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("expired", "Expired"),
                            ("consumed", "Consumed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("rejection_reason", models.CharField(blank=True, max_length=500)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approved_tool_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_requests",
                        to="core.pluginconnection",
                    ),
                ),
                (
                    "installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_requests",
                        to="core.plugininstallation",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tool_approvals",
                        to="core.project",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tool_approvals",
                        to="core.task",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tool_approvals",
                        to="core.tenant",
                    ),
                ),
                (
                    "tool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_requests",
                        to="core.plugintool",
                    ),
                ),
            ],
            options={"ordering": ["-requested_at"]},
        ),
        migrations.AddConstraint(
            model_name="toolapprovalrequest",
            constraint=models.UniqueConstraint(
                fields=("task", "call_id"),
                name="uniq_tool_approval_task_call",
            ),
        ),
        migrations.AddIndex(
            model_name="toolapprovalrequest",
            index=models.Index(
                fields=["tenant", "project", "status", "expires_at"],
                name="tool_approval_scope_idx",
            ),
        ),
    ]
