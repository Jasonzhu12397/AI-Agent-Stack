import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_deployment_checkpoint"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=120)),
                ("resource_type", models.CharField(max_length=80)),
                ("resource_id", models.CharField(blank=True, max_length=64)),
                ("outcome", models.CharField(choices=[("success", "Success"), ("denied", "Denied"), ("failure", "Failure")], default="success", max_length=16)),
                ("request_id", models.CharField(blank=True, max_length=64)),
                ("remote_addr", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=256)),
                ("metadata", models.JSONField(default=dict)),
                ("previous_hash", models.CharField(blank=True, max_length=64)),
                ("event_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="core.project")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audit_events", to="core.tenant")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["tenant", "-created_at"], name="audit_tenant_time_idx"),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["tenant", "event_type", "-created_at"], name="audit_tenant_type_idx"),
        ),
    ]
