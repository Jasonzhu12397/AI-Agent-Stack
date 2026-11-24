"""Align every authorized provider with the platform catalogue's current models.

The preset lists shipped with the console move as platforms ship new flagship
models. Without this, members who authorized earlier would keep calling models
the platform has retired, so the migration seeds the current list and disables
rows that were only ever seeded from the old one.
"""

from django.db import migrations


def refresh(apps, schema_editor):
    # The helper works with the live model (relations, choices, managers), so
    # only the primary keys come from the migration's historical snapshot.
    HistoricalProvider = apps.get_model("core", "ModelProvider")
    from core.models import ModelProvider
    from core.provider_accounts import refresh_preset_models

    pks = list(
        HistoricalProvider.objects.filter(auth_mode="user_account")
        .exclude(preset_key__isnull=True)
        .exclude(preset_key="")
        .values_list("pk", flat=True)
    )
    for pk in pks:
        provider = ModelProvider.objects.filter(pk=pk).first()
        if provider is not None:
            refresh_preset_models(provider)


class Migration(migrations.Migration):

    dependencies = [("core", "0058_provideroauthstate")]

    operations = [migrations.RunPython(refresh, migrations.RunPython.noop)]
