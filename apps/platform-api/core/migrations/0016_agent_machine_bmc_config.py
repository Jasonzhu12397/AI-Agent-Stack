from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_agent_machine_inventory"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentmachine",
            name="bmc_endpoint",
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="agentmachine",
            name="bmc_trust_bundle_ref",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="agentmachine",
            name="bmc_tls_verify",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="agentmachine",
            name="bmc_configured_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentmachine",
            name="bmc_identity_evidence",
            field=models.JSONField(default=dict),
        ),
    ]
