import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0018_agentrun_execution_state")]

    operations = [
        migrations.CreateModel(
            name="TenantTokenBudget",
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
                ("daily_limit", models.PositiveBigIntegerField(default=100000)),
                ("monthly_limit", models.PositiveBigIntegerField(default=1000000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="token_budget",
                        to="core.tenant",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TenantTokenUsage",
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
                ("idempotency_key", models.CharField(max_length=160)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("reserved", "Reserved"),
                            ("settled", "Settled"),
                            ("released", "Released"),
                        ],
                        default="reserved",
                        max_length=16,
                    ),
                ),
                ("reserved_tokens", models.PositiveBigIntegerField(default=0)),
                ("consumed_tokens", models.PositiveBigIntegerField(default=0)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agent_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="token_usage",
                        to="core.agentrun",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="token_usage",
                        to="core.task",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="token_usage",
                        to="core.tenant",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="tenanttokenusage",
            constraint=models.UniqueConstraint(
                fields=("tenant", "idempotency_key"),
                name="uniq_tenant_token_usage_idempotency",
            ),
        ),
        migrations.AddIndex(
            model_name="tenanttokenusage",
            index=models.Index(
                fields=["tenant", "status", "created_at"],
                name="tenant_token_usage_period_idx",
            ),
        ),
        migrations.AlterField(
            model_name="tenanttokenusage",
            name="agent_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="token_usage",
                to="core.agentrun",
            ),
        ),
        migrations.AddField(
            model_name="tenanttokenusage",
            name="workflow_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="token_usage",
                to="core.workflowrun",
            ),
        ),
        migrations.AddConstraint(
            model_name="tenanttokenusage",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(agent_run__isnull=False, workflow_run__isnull=True)
                    | models.Q(agent_run__isnull=True, workflow_run__isnull=False)
                ),
                name="tenant_token_usage_exactly_one_run",
            ),
        ),
    ]
