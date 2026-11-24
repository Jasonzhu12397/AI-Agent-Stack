"""Align every authorized provider with the platform catalogue's current models.

The preset lists shipped with the console move as platforms ship new flagship
models. Without this, members who authorized earlier would keep calling models
the platform has retired, so the migration seeds the current list and disables
rows that were only ever seeded from the old one.
"""

from django.db import migrations


def refresh(apps, schema_editor):
    ModelProvider = apps.get_model("core", "ModelProvider")
    from core.provider_accounts import refresh_preset_models

    for provider in ModelProvider.objects.filter(
        auth_mode="user_account"
    ).exclude(preset_key__isnull=True).exclude(preset_key=""):
        refresh_preset_models(provider)


class Migration(migrations.Migration):

    dependencies = [("core", "0058_provideroauthstate")]

    operations = [migrations.RunPython(refresh, migrations.RunPython.noop)]
