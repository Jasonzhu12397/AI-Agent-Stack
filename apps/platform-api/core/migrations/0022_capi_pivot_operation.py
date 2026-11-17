import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_eph_enrollment_recovery"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClusterDeploymentPivotOperation",
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
                ("operation_id", models.UUIDField(editable=False, unique=True)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("prepared", "Prepared"),
                            ("waiting_approval", "Waiting for approval"),
                            ("waiting_target_ack", "Waiting for target acknowledgement"),
                            ("ready", "Ready"),
                            ("executing", "Executing"),
                            ("verifying", "Verifying"),
                            ("uncertain", "Uncertain"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("manual_recovery", "Manual recovery required"),
                        ],
                        default="prepared",
                        max_length=24,
                    ),
                ),
                ("source_identity", models.JSONField(default=dict)),
                ("inventory", models.JSONField(default=list)),
                ("inventory_sha256", models.CharField(max_length=64)),
                ("approval_hash", models.CharField(blank=True, max_length=64)),
                ("target_ack", models.JSONField(blank=True, null=True)),
                ("target_identity", models.JSONField(blank=True, null=True)),
                ("journal", models.JSONField(default=dict)),
                ("version", models.PositiveBigIntegerField(default=0)),
                ("last_error", models.JSONField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "deployment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capi_pivot_operation",
                        to="core.clusterdeployment",
                    ),
                ),
                (
                    "source_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capi_pivot_source_operations",
                        to="core.agentnode",
                    ),
                ),
                (
                    "target_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capi_pivot_target_operations",
                        to="core.agentnode",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="clusterdeploymentpivotoperation",
            index=models.Index(
                fields=["state", "updated_at"],
                name="capi_pivot_state_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="clusterdeploymentpivotoperation",
            index=models.Index(
                fields=["source_node", "state"],
                name="capi_pivot_source_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="clusterdeploymentpivotoperation",
            index=models.Index(
                fields=["target_node", "state"],
                name="capi_pivot_target_idx",
            ),
        ),
        migrations.CreateModel(
            name="ClusterDeploymentPivotEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("sequence", models.PositiveBigIntegerField()),
                ("event_type", models.CharField(max_length=64)),
                ("from_state", models.CharField(blank=True, max_length=24)),
                ("to_state", models.CharField(max_length=24)),
                ("evidence", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor_node",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capi_pivot_events",
                        to="core.agentnode",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="core.clusterdeploymentpivotoperation",
                    ),
                ),
            ],
            options={"ordering": ["sequence"]},
        ),
        migrations.AddConstraint(
            model_name="clusterdeploymentpivotevent",
            constraint=models.UniqueConstraint(
                fields=("operation", "sequence"),
                name="uniq_capi_pivot_event_sequence",
            ),
        ),
        migrations.AddIndex(
            model_name="clusterdeploymentpivotevent",
            index=models.Index(
                fields=["operation", "created_at"],
                name="capi_pivot_event_time_idx",
            ),
        ),
    ]
