import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_task"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ModelProvider",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "provider_type",
                    models.CharField(
                        choices=[
                            ("fake", "Fake model"),
                            ("openai_compatible", "OpenAI compatible"),
                        ],
                        max_length=32,
                    ),
                ),
                ("base_url", models.URLField(blank=True, max_length=500)),
                ("credential_ref", models.CharField(blank=True, max_length=160)),
                ("config", models.JSONField(default=dict)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_model_providers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="model_providers",
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "name"),
                        name="uniq_model_provider_name_per_tenant",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ModelConfig",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("model_name", models.CharField(max_length=160)),
                ("display_name", models.CharField(max_length=160)),
                ("capabilities", models.JSONField(default=list)),
                ("context_window", models.PositiveIntegerField(default=32768)),
                ("max_output_tokens", models.PositiveIntegerField(default=4096)),
                (
                    "input_cost_per_million",
                    models.DecimalField(decimal_places=6, default=0, max_digits=12),
                ),
                (
                    "output_cost_per_million",
                    models.DecimalField(decimal_places=6, default=0, max_digits=12),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "provider",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="models",
                        to="core.modelprovider",
                    ),
                ),
            ],
            options={
                "ordering": ["provider__name", "display_name", "-version"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("provider", "model_name", "version"),
                        name="uniq_model_config_version",
                    )
                ],
            },
        ),
    ]
