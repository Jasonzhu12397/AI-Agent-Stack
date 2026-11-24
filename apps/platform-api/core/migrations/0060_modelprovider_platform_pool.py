"""Add the shared-pool auth mode: one operator key for every member."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_refresh_preset_models"),
    ]

    operations = [
        migrations.AlterField(
            model_name="modelprovider",
            name="auth_mode",
            field=models.CharField(
                choices=[
                    ("tenant_secret", "Tenant credential"),
                    ("user_account", "Linked user account"),
                    ("platform_pool", "Platform credential (no per-user key)"),
                ],
                default="tenant_secret",
                max_length=32,
            ),
        ),
    ]
