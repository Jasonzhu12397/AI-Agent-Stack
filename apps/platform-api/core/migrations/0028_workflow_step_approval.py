from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_agentteam_agentteamversion_agentteamrun_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflowsteprun",
            name="status",
            field=models.CharField(
                choices=[
                    ("running", "Running"),
                    ("awaiting_approval", "Awaiting approval"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                max_length=24,
            ),
        ),
    ]
