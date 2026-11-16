from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0020_alter_clusterdeployment_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentnode",
            name="pending_enrollment_token_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="pending_enrollment_csr_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="pending_enrollment_response_encrypted",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="agentnode",
            name="pending_enrollment_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
