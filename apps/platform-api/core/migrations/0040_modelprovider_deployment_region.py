from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the deployment region classification used by the purchase gate.

    ``""`` means unclassified and fails closed; only ``domestic`` providers are
    purchasable. The field carries no vendor enumeration.
    """

    dependencies = [
        ("core", "0034_agentcommand_expired_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="modelprovider",
            name="deployment_region",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
