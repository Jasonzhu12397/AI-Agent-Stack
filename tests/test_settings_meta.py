"""Metadata endpoints: shortcut registry, about payload, feedback audit trail."""

import pytest
from core.models import AuditEvent, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from packages.version import project_version
from rest_framework.test import APIClient


@pytest.fixture
def feedback_scope(db):
    user = get_user_model().objects.create_user(
        username="feedback-user", password="test-password"
    )
    tenant = Tenant.objects.create(name="Feedback tenant", slug="feedback-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "client": client}


@pytest.mark.django_db
def test_shortcut_registry_and_conflict(feedback_scope):
    client = feedback_scope["client"]

    registry = client.get("/api/v1/meta/shortcuts/")
    assert registry.status_code == 200
    entries = registry.data["results"]
    assert entries
    required = {"id", "default_combination", "scope", "description"}
    for entry in entries:
        assert required <= set(entry)

    conflict = client.put(
        "/api/v1/users/me/preferences/shortcuts/",
        {
            "values": {
                "sidebar.toggle": "Mod+B",
                "command.palette": "Mod+B",
            }
        },
        format="json",
    )
    assert conflict.status_code == 409
    error = conflict.data["error"]
    assert error["code"] == "SHORTCUT_CONFLICT"

    invalid = client.put(
        "/api/v1/users/me/preferences/shortcuts/",
        {"values": {"sidebar.toggle": "not a combination"}},
        format="json",
    )
    assert invalid.status_code == 400
    error = invalid.data["error"]
    assert error["code"] == "SHORTCUT_INVALID_COMBINATION"


@pytest.mark.django_db
def test_about_version_payload(feedback_scope):
    client = feedback_scope["client"]

    about = client.get("/api/v1/meta/about/")
    assert about.status_code == 200
    payload = about.data
    assert payload["version"] == project_version()
    assert "build" in payload
    assert "licenses" in payload

    serialized = str(payload).lower()
    for forbidden in {"secret", "password", "token", "private_key", "api_key"}:
        assert forbidden not in serialized


@pytest.mark.django_db
def test_feedback_create_written_to_audit(feedback_scope):
    client = feedback_scope["client"]
    tenant = feedback_scope["tenant"]
    user = feedback_scope["user"]

    created = client.post(
        f"/api/v1/tenants/{tenant.id}/feedback/",
        {
            "category": "bug",
            "severity": "medium",
            "body": "Shortcuts page misbehaves on narrow screens.",
        },
        format="json",
    )
    assert created.status_code == 201
    assert created.data["category"] == "bug"
    assert created.data["severity"] == "medium"

    events = AuditEvent.objects.filter(
        tenant=tenant, actor=user, event_type="feedback.created"
    )
    assert events.count() == 1
    metadata = events.first().metadata
    assert "body" not in metadata or len(str(metadata.get("body", ""))) == 0

    listed = client.get(f"/api/v1/tenants/{tenant.id}/feedback/")
    assert listed.status_code == 200
    assert listed.data["results"][0]["body"].startswith("Shortcuts page")
