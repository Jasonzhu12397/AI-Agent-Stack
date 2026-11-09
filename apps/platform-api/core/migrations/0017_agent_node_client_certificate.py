from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_agent_machine_bmc_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentnode",
            name="client_certificate_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="client_certificate_bound_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="client_certificate_revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="agentnode",
            constraint=models.UniqueConstraint(
                fields=("client_certificate_fingerprint",),
                condition=~models.Q(client_certificate_fingerprint=""),
                name="uniq_agent_client_cert_fingerprint",
            ),
        ),
    ]
