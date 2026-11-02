import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_workflow"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentNode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("node_type", models.CharField(choices=[("eph_node", "EPH node"), ("target_cluster", "Target cluster")], max_length=24)),
                ("cluster_name", models.CharField(blank=True, max_length=120)),
                ("agent_version", models.CharField(blank=True, max_length=40)),
                ("capabilities", models.JSONField(default=list)),
                ("labels", models.JSONField(default=dict)),
                ("credential_hash", models.CharField(max_length=64)),
                ("status", models.CharField(choices=[("offline", "Offline"), ("online", "Online"), ("revoked", "Revoked")], default="offline", max_length=16)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("last_seen_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="enrolled_agent_nodes", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_nodes", to="core.project")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_nodes", to="core.tenant")),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(fields=("project", "name"), name="uniq_agent_node_name_per_project"),
                ],
                "indexes": [
                    models.Index(fields=["tenant", "project", "node_type"], name="agent_node_scope_idx"),
                    models.Index(fields=["status", "last_seen_at"], name="agent_node_status_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="AgentCommand",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("command_type", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("waiting_approval", "Waiting approval"), ("queued", "Queued"), ("running", "Running"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("cancelled", "Cancelled")], max_length=24)),
                ("idempotency_key", models.CharField(max_length=128)),
                ("plan_hash", models.CharField(max_length=64)),
                ("approved_plan_hash", models.CharField(blank=True, max_length=64)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3)),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("result", models.JSONField(blank=True, null=True)),
                ("error", models.JSONField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_agent_commands", to=settings.AUTH_USER_MODEL)),
                ("node", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="commands", to="core.agentnode")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_commands", to="core.project")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="requested_agent_commands", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_commands", to="core.tenant")),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.UniqueConstraint(fields=("tenant", "idempotency_key"), name="uniq_agent_command_idempotency"),
                ],
                "indexes": [
                    models.Index(fields=["node", "status", "created_at"], name="agent_command_queue_idx"),
                    models.Index(fields=["tenant", "project", "created_at"], name="agent_command_scope_idx"),
                ],
            },
        ),
    ]
