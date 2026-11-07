import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_agent_nodes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClusterDeployment",
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
                ("cluster_name", models.SlugField(max_length=63)),
                ("kubernetes_version", models.CharField(max_length=32)),
                ("spec", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("preflight_queued", "Preflight queued"),
                            ("planning", "Planning"),
                            ("plan_ready", "Plan ready"),
                            ("waiting_approval", "Waiting approval"),
                            ("provisioning", "Provisioning"),
                            ("control_plane_ready", "Control plane ready"),
                            ("workers_ready", "Workers ready"),
                            ("target_agent_pending", "Target agent pending"),
                            ("postflight", "Postflight"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="preflight_queued",
                        max_length=32,
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=128)),
                ("plan_hash", models.CharField(blank=True, max_length=64)),
                ("preflight_result", models.JSONField(blank=True, null=True)),
                ("plan_result", models.JSONField(blank=True, null=True)),
                ("error", models.JSONField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "eph_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cluster_deployments",
                        to="core.agentnode",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cluster_deployments",
                        to="core.project",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cluster_deployments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target_node",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="target_cluster_deployments",
                        to="core.agentnode",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cluster_deployments",
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "idempotency_key"),
                        name="uniq_cluster_deployment_idempotency",
                    ),
                ],
                "indexes": [
                    models.Index(
                        fields=["tenant", "project", "status"],
                        name="cluster_deploy_scope_idx",
                    ),
                    models.Index(
                        fields=["eph_node", "status", "created_at"],
                        name="cluster_deploy_node_idx",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="agentcommand",
            name="deployment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="commands",
                to="core.clusterdeployment",
            ),
        ),
    ]
