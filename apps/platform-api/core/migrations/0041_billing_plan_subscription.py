import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Billing plans and tenant subscriptions."""

    dependencies = [
        ("core", "0040_modelprovider_deployment_region"),
    ]

    operations = [
        migrations.CreateModel(
            name="Plan",
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
                ("code", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                (
                    "interval",
                    models.CharField(
                        choices=[
                            ("monthly", "Monthly"),
                            ("yearly", "Yearly"),
                        ],
                        default="monthly",
                        max_length=16,
                    ),
                ),
                ("price_cents", models.PositiveBigIntegerField(default=0)),
                ("currency", models.CharField(default="CNY", max_length=8)),
                ("token_quota_daily", models.PositiveBigIntegerField(default=0)),
                ("token_quota_monthly", models.PositiveBigIntegerField(default=0)),
                ("features", models.JSONField(default=list)),
                (
                    "region_scope",
                    models.CharField(
                        choices=[("domestic", "Domestic")],
                        default="domestic",
                        max_length=16,
                    ),
                ),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["price_cents", "code"]},
        ),
        migrations.CreateModel(
            name="Subscription",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("active", "Active"),
                            ("past_due", "Past due"),
                            ("cancelled", "Cancelled"),
                            ("expired", "Expired"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                (
                    "current_period_start",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "current_period_end",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("cancel_at_period_end", models.BooleanField(default=False)),
                (
                    "renewal_mode",
                    models.CharField(
                        choices=[("manual", "Manual")],
                        default="manual",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to="core.plan",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subscriptions",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.UniqueConstraint(
                fields=("tenant", "plan"),
                name="uniq_subscription_tenant_plan",
            ),
        ),
    ]
