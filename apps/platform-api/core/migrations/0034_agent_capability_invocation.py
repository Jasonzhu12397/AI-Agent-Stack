import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_agent_plugin_manifest"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentCapabilityInvocation",
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
                ("capability", models.CharField(max_length=100)),
                ("protocol", models.CharField(max_length=16)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("success", "Success"),
                            ("failed", "Failed"),
                            ("rejected", "Rejected"),
                        ],
                        max_length=16,
                    ),
                ),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="capability_invocations",
                        to="core.agentplugininstallation",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=("installation", "capability", "created_at"),
                        name="agent_cap_invocation_idx",
                    )
                ],
            },
        ),
    ]
