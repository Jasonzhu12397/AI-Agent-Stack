import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_tool_approval_rejection_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentMachine",
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
                ("vendor", models.CharField(blank=True, max_length=256)),
                ("product_name", models.CharField(blank=True, max_length=256)),
                ("system_uuid", models.CharField(blank=True, max_length=128)),
                ("identity_material", models.JSONField(default=dict)),
                ("identity_fingerprint", models.CharField(blank=True, max_length=64)),
                (
                    "identity_status",
                    models.CharField(
                        choices=[
                            ("unverified", "Unverified"),
                            ("candidate", "Candidate"),
                            ("verified", "Verified"),
                            ("conflict", "Conflict"),
                        ],
                        default="unverified",
                        max_length=16,
                    ),
                ),
                ("identity_conflicts", models.JSONField(default=list)),
                ("hardware_inventory", models.JSONField(default=dict)),
                ("inventory_source", models.CharField(default="agent_report", max_length=48)),
                ("observed_at", models.DateTimeField(blank=True, null=True)),
                ("bmc_credential_ref", models.CharField(blank=True, max_length=160)),
                ("bmh_name", models.CharField(blank=True, max_length=253)),
                ("capi_machine_name", models.CharField(blank=True, max_length=253)),
                ("kubernetes_node_name", models.CharField(blank=True, max_length=253)),
                ("lifecycle_state", models.CharField(default="discovered", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agent_node",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="machine",
                        to="core.agentnode",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="machines",
                        to="core.project",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="machines",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["agent_node__name"]},
        ),
        migrations.AddIndex(
            model_name="agentmachine",
            index=models.Index(
                fields=["tenant", "project", "identity_status"],
                name="machine_identity_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="agentmachine",
            index=models.Index(
                fields=["tenant", "project", "observed_at"],
                name="machine_observed_idx",
            ),
        ),
    ]
