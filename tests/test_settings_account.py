"""Account profile, identities and session management endpoints."""

import pytest
from core.models import AuditEvent, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from rest_framework.test import APIClient


@pytest.fixture
def account_user(db):
    user_model = get_user_model()
    return user_model.objects.create_user(
        username="account-user",
        password="test-password",
        email="account-user@example.test",
    )


def _session_for(user, key):
    from django.contrib.sessions.models import Session

    store = SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    store.save()
    original = Session.objects.get(session_key=store.session_key)
    Session.objects.create(
        session_key=key,
        session_data=original.session_data,
        expire_date=original.expire_date,
    )
    original.delete()
    return key


@pytest.mark.django_db
def test_account_profile_update_and_email_unique(account_user):
    other = get_user_model().objects.create_user(
        username="account-other",
        password="test-password",
        email="other@example.test",
    )
    client = APIClient()
    client.force_authenticate(user=account_user)

    updated = client.patch(
        "/api/v1/users/me/",
        {
            "display_name": "Account User",
            "timezone": "America/Chicago",
            "locale": "en",
        },
        format="json",
    )
    assert updated.status_code == 200
    assert updated.data["display_name"] == "Account User"
    assert updated.data["timezone"] == "America/Chicago"
    assert updated.data["locale"] == "en"

    reread = client.get("/api/v1/users/me/")
    assert reread.status_code == 200
    assert reread.data["display_name"] == "Account User"

    conflict = client.patch(
        "/api/v1/users/me/",
        {"email": other.email},
        format="json",
    )
    assert conflict.status_code in {400, 409}
    error = conflict.data["error"]
    assert error["code"] == "ACCOUNT_EMAIL_TAKEN"

    own_email = client.patch(
        "/api/v1/users/me/",
        {"email": "account-user+new@example.test"},
        format="json",
    )
    assert own_email.status_code == 200
    assert own_email.data["email"] == "account-user+new@example.test"


@pytest.mark.django_db
def test_account_sessions_revoke_idempotent_and_audited(account_user):
    tenant = Tenant.objects.create(name="Session tenant", slug="session-tenant")
    TenantMembership.objects.create(tenant=tenant, user=account_user, role="owner")
    _session_for(account_user, "session-one")
    _session_for(account_user, "session-two")

    client = APIClient()
    client.force_authenticate(user=account_user)

    listed = client.get("/api/v1/users/me/sessions/")
    assert listed.status_code == 200
    keys = {item["session_key"] for item in listed.data["results"]}
    assert "session-one" in keys
    assert "session-two" in keys
    assert all("is_current" in item for item in listed.data["results"])

    revoked = client.post("/api/v1/users/me/sessions/session-one/revoke/")
    assert revoked.status_code == 200

    listed_after = client.get("/api/v1/users/me/sessions/")
    remaining = {item["session_key"] for item in listed_after.data["results"]}
    assert "session-one" not in remaining
    assert "session-two" in remaining

    replay = client.post("/api/v1/users/me/sessions/session-one/revoke/")
    assert replay.status_code == 200

    events = AuditEvent.objects.filter(
        actor=account_user, event_type="session.revoked"
    ).order_by("created_at", "id")
    assert events.count() >= 1
    for event in events:
        assert "password" not in event.metadata
        assert "hash" not in event.metadata

    identities = client.get("/api/v1/users/me/identities/")
    assert identities.status_code == 200
    payload = identities.data
    assert isinstance(payload["has_password"], bool)
    assert isinstance(payload["bound_identities"], int)
    serialized = str(payload).lower()
    assert "hash" not in serialized
    assert "set_password" not in serialized
    assert "pbkdf2" not in serialized
