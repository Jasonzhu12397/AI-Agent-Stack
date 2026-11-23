import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Append-only ledger for quota reclaimed on refunds.

    Records the tokens clawed back per refund slice, the grant and order the
    refund originated from, the budget limit before/after the reclaim and the
    unique provider refund reference that makes the row idempotent. Also adds
    the cumulative ``refunded_cents`` balance on the order.
    """

    dependencies = [
        ("core", "0049_payment_transaction_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="refunded_cents",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("pending", "Pending"),
                    ("paid", "Paid"),
                    ("failed", "Failed"),
                    ("closed", "Closed"),
                    ("partial_refunded", "Partially Refunded"),
                    ("refunded", "Refunded"),
                ],
                default="created",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="EntitlementReclaim",
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
                    "refund_reference",
                    models.CharField(max_length=160, unique=True),
                ),
                ("refund_cents", models.PositiveBigIntegerField(default=0)),
                ("tokens", models.PositiveBigIntegerField(default=0)),
                (
                    "monthly_limit_before",
                    models.PositiveBigIntegerField(default=0),
                ),
                (
                    "monthly_limit_after",
                    models.PositiveBigIntegerField(default=0),
                ),
                (
                    "reason",
                    models.CharField(blank=True, default="", max_length=160),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "grant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reclaims",
                        to="core.entitlementgrant",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reclaims",
                        to="core.order",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entitlement_reclaims",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
