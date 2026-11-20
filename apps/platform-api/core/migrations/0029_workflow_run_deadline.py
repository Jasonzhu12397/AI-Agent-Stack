from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_workflow_step_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowrun",
            name="deadline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
