import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0069_cloud_vm_instances")]

    operations = [
        migrations.AddField(
            model_name="modelconfig",
            name="fallback_model",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fallback_for",
                to="core.modelconfig",
            ),
        ),
    ]
