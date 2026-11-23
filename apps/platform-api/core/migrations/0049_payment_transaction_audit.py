import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Append-only ledger for outbound payment requests and callbacks.

    Records provider requests, verified and rejected callbacks, amount
    mismatches and refund receipts. Metadata is sanitized upstream and never
    carries private keys, symmetric keys, certificates or PEM material.
    """

    dependencies = [
        ("core", "0048_subscription_lifecycle"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentTransactionAudit",
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
                    "channel",
                    models.CharField(max_length=16),
                ),
                (
                    "direction",
                    models.CharField(
                        choices=[
                            ("request", "Request"),
                            ("callback", "Callback"),
                        ],
                        max_length=16,
                    ),
                ),
                ("event_type", models.CharField(max_length=80)),
                ("status", models.CharField(blank=True, default="", max_length=32)),
                ("amount_cents", models.PositiveBigIntegerField(default=0)),
                (
                    "provider_trade_no",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("error_code", models.CharField(blank=True, default="", max_length=80)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="payment_transaction_audits",
                        to="core.order",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="payment_transaction_audits",
                        to="core.tenant",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
