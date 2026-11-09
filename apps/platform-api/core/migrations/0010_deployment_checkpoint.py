import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_knowledge_base"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClusterDeploymentCheckpoint",
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
                ("generation", models.PositiveIntegerField(default=1)),
                ("sequence", models.PositiveBigIntegerField(default=0)),
                ("phase", models.CharField(max_length=64)),
                ("data", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agent_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deployment_checkpoints",
                        to="core.agentnode",
                    ),
                ),
                (
                    "deployment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="checkpoint",
                        to="core.clusterdeployment",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
    ]
