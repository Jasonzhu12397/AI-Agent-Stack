from django.db import migrations, models


class Migration(migrations.Migration):
    """Allow ad-hoc (model test / RAG embedding) calls to write token usage rows.

    Run-owned reservations keep their exactly-one-run linkage; ad-hoc rows carry
    a free-form ``scope_ref`` instead and no longer require a Task. Only the
    exactly-one-run check constraint is relaxed; existing rows and behavior are
    unchanged.
    """

    dependencies = [
        ("core", "0045_billing_order_entitlement_grant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenanttokenusage",
            name="task",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="token_usage",
                to="core.task",
            ),
        ),
        migrations.AddField(
            model_name="tenanttokenusage",
            name="scope_ref",
            field=models.CharField(blank=True, max_length=160, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="tenanttokenusage",
            name="tenant_token_usage_exactly_one_run",
        ),
        migrations.AddConstraint(
            model_name="tenanttokenusage",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(agent_run__isnull=False, workflow_run__isnull=True)
                    | models.Q(agent_run__isnull=True, workflow_run__isnull=False)
                    | models.Q(
                        scope_ref__isnull=False,
                        agent_run__isnull=True,
                        workflow_run__isnull=True,
                    )
                ),
                name="tenant_token_usage_exactly_one_run",
            ),
        ),
    ]
