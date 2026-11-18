from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0020_token_usage_settlement_timestamp")]

    operations = [
        migrations.RemoveIndex(
            model_name="tenanttokenusage",
            name="tenant_token_usage_period_idx",
        ),
        migrations.AddIndex(
            model_name="tenanttokenusage",
            index=models.Index(
                fields=["tenant", "status", "settled_at"],
                name="tenant_token_settled_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="tenanttokenusage",
            index=models.Index(
                fields=["tenant", "status", "expires_at"],
                name="tenant_token_reserved_idx",
            ),
        ),
    ]
