from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_eph_certificate_renewal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clusterdeployment",
            name="status",
            field=models.CharField(
                choices=[
                    ("preflight_queued", "Preflight queued"),
                    ("planning", "Planning"),
                    ("plan_ready", "Plan ready"),
                    ("waiting_approval", "Waiting approval"),
                    ("provisioning", "Provisioning"),
                    ("control_plane_ready", "Control plane ready"),
                    ("workers_ready", "Workers ready"),
                    ("target_agent_pending", "Target agent pending"),
                    ("postflight", "Postflight"),
                    ("reconciliation_required", "Reconciliation required"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="preflight_queued",
                max_length=32,
            ),
        ),
    ]
