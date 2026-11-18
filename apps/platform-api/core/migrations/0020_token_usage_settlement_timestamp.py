from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0019_tenant_token_budget")]

    operations = [
        migrations.AddField(
            model_name="tenanttokenusage",
            name="settled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
