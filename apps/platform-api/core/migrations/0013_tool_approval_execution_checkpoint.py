from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_tool_approval_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="toolapprovalrequest",
            name="execution_checkpoint",
            field=models.JSONField(default=dict),
        ),
    ]
