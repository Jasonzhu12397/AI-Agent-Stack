from django.db import migrations, models


def keep_one_healthy_connection_per_installation(apps, schema_editor):
    PluginConnection = apps.get_model("core", "PluginConnection")
    database = schema_editor.connection.alias
    installation_ids = (
        PluginConnection.objects.using(database)
        .filter(status="healthy")
        .values_list("installation_id", flat=True)
        .distinct()
    )
    for installation_id in installation_ids.iterator():
        healthy_ids = list(
            PluginConnection.objects.using(database)
            .filter(installation_id=installation_id, status="healthy")
            .order_by("-updated_at", "-created_at", "-pk")
            .values_list("pk", flat=True)
        )
        if len(healthy_ids) > 1:
            PluginConnection.objects.using(database).filter(pk__in=healthy_ids[1:]).update(
                status="error"
            )


class Migration(migrations.Migration):
    dependencies = [("core", "0021_token_usage_period_indexes")]

    operations = [
        migrations.RunPython(
            keep_one_healthy_connection_per_installation,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="pluginconnection",
            constraint=models.UniqueConstraint(
                fields=("installation",),
                condition=models.Q(status="healthy"),
                name="uniq_healthy_plugin_connection",
            ),
        ),
    ]
