"""Subscription lifecycle: trial length, post-expiry fallback quota, trialing state.

Adds the plan fields needed to run a free trial and to reset the tenant budget
to a post-expiry fallback quota, and registers the ``trialing`` subscription
status in the state machine. No existing table or constraint is rewritten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_token_usage_scope_ref"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="trial_days",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="plan",
            name="token_quota_daily_expired",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="plan",
            name="token_quota_monthly_expired",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("trialing", "Trialing"),
                    ("active", "Active"),
                    ("past_due", "Past due"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
