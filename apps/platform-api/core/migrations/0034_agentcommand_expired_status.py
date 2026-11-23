from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the ``expired`` terminal status to agent commands.

    An approved rollback that no agent ever claimed past its protection horizon
    transitions to ``expired`` through the sweep and releases its snapshot
    protection. The choice list on the status field follows the model; no data
    backfill is needed because existing rows keep their current status.
    """

    dependencies = [
        ("core", "0033_backfill_command_deployment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agentcommand",
            name="status",
            field=models.CharField(
                choices=[
                    ("waiting_approval", "Waiting approval"),
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                ],
                max_length=24,
            ),
        ),
    ]
