import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_agent_node_client_certificate"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EPHSeedArtifact",
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
                ("encrypted_payload", models.TextField()),
                ("download_token_hash", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("downloaded_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("agent_node", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="seed_artifacts",
                    to="core.agentnode",
                )),
                ("created_by", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="created_eph_seed_artifacts",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["agent_node", "expires_at", "downloaded_at"],
                        name="eph_seed_lifecycle_idx",
                    ),
                ],
            },
        ),
    ]
