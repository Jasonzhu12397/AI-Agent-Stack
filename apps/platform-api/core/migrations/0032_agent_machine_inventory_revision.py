from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_infrastructure_network_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentmachine",
            name="inventory_revision",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
