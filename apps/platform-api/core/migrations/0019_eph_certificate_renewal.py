from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_eph_seed_artifact"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentnode",
            name="pending_client_certificate_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="pending_client_certificate_bound_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="last_activated_client_certificate_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
