import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Purchase orders and append-only entitlement grant ledger."""

    dependencies = [
        ("core", "0041_billing_plan_subscription"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Order",
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
                            ("created", "Created"),
                            ("pending", "Pending"),
                            ("paid", "Paid"),
                            ("failed", "Failed"),
                            ("closed", "Closed"),
                            ("refunded", "Refunded"),
                        ],
                        default="created",
                        max_length=16,
                    ),
                ),
                ("amount_cents", models.PositiveBigIntegerField(default=0)),
                ("currency", models.CharField(default="CNY", max_length=8)),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("wechat", "WeChat Pay"),
                            ("alipay", "Alipay"),
                        ],
                        max_length=16,
                    ),
                ),
                ("provider_trade_no", models.CharField(blank=True, default="", max_length=128)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("idempotency_key", models.CharField(max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_orders",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="core.plan",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="orders",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="EntitlementGrant",
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
                    "period",
                    models.CharField(
                        choices=[
                            ("daily", "Daily"),
                            ("monthly", "Monthly"),
                        ],
                        max_length=16,
                    ),
                ),
                ("tokens", models.PositiveBigIntegerField(default=0)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("reason", models.CharField(max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grant",
                        to="core.order",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grants",
                        to="core.subscription",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entitlement_grants",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                fields=("tenant", "idempotency_key"),
                name="uniq_order_idempotency_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                condition=models.Q(provider_trade_no__gt=""),
                fields=("channel", "provider_trade_no"),
                name="uniq_order_provider_trade_no",
            ),
        ),
    ]
