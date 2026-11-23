from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_agent_machine_inventory_revision"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentplugindefinition",
            name="manifest",
            field=models.JSONField(default=dict),
        ),
    ]
