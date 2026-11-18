from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_agent_node_client_certificate"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="execution_state",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
