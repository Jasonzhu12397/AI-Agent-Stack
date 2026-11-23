import json
from datetime import datetime, timedelta

import pytest
from core.models import AuditEvent, Project, ProjectMembership, Tenant, TenantMembership
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone
from rest_framework.test import APIClient


def _create_session(user, *, expire_date=None):
    """Persist a real DB session row bound to ``user`` through SessionStore."""
    store = SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    store.save()
    session_key = store.session_key
    if expire_date is None:
        expire_date = timezone.now() + timedelta(days=7)
    Session.objects.filter(session_key=session_key).update(expire_date=expire_date)
    return session_key


@pytest.fixture
def session_workspace(db):
    owner = get_user_model().objects.create_user(
        username="ps-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="PS Tenant", slug="ps-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=owner, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="PS Project", slug="ps-project"
    )
    ProjectMembership.objects.create(
        project=project, user=owner, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    return {"user": owner, "tenant": tenant, "project": project, "client": client}


def _list_url(tenant):
    return f"/api/v1/tenants/{tenant.id}/privacy/sessions/"


def _revoke_url(tenant, session_key):
    return f"/api/v1/tenants/{tenant.id}/privacy/sessions/{session_key}/revoke/"


@pytest.mark.django_db
def test_session_list_isolated_to_current_user(session_workspace):
    owner = session_workspace["user"]
    owner_keys = [_create_session(owner), _create_session(owner)]

    other = get_user_model().objects.create_user(
        username="ps-other", password="test-password"
    )
    other_tenant = Tenant.objects.create(name="PS Other Tenant", slug="ps-other-tenant")
    TenantMembership.objects.create(
        tenant=other_tenant, user=other, role=TenantMembership.Role.OWNER
    )
    other_key = _create_session(other)

    response = session_workspace["client"].get(_list_url(session_workspace["tenant"]))
    assert response.status_code == 200
    results = response.data["results"]
    assert len(results) == 2

    payload = response.content.decode()
    for full_key in owner_keys:
        assert full_key not in payload
    assert other_key not in payload

    for entry in results:
        assert set(entry) == {
            "session_key_masked",
            "is_current",
            "expire_date",
            "created_at",
            "created_at_source",
            "last_activity",
            "last_activity_source",
        }
        assert entry["created_at_source"] == "derived_from_expire_date"
        assert entry["last_activity"] is None
        assert entry["last_activity_source"] == "unavailable"
        assert "********" in entry["session_key_masked"]


@pytest.mark.django_db
def test_created_at_is_derived_from_expire_date(session_workspace):
    cookie_age = int(getattr(settings, "SESSION_COOKIE_AGE", 1209600))
    expire_date = timezone.now() + timedelta(days=7)
    session_key = _create_session(session_workspace["user"], expire_date=expire_date)

    response = session_workspace["client"].get(_list_url(session_workspace["tenant"]))
    assert response.status_code == 200
    entry = next(
        item for item in response.data["results"]
        if item["session_key_masked"].endswith(session_key[-4:])
    )

    created_at = datetime.fromisoformat(entry["created_at"])
    expires_at = datetime.fromisoformat(entry["expire_date"])
    assert int((expires_at - created_at).total_seconds()) == cookie_age
    assert entry["created_at_source"] == "derived_from_expire_date"


@pytest.mark.django_db
def test_revoke_is_idempotent_and_audited(session_workspace):
    session_key = _create_session(session_workspace["user"])
    url = _revoke_url(session_workspace["tenant"], session_key)

    first = session_workspace["client"].post(url, {}, format="json")
    assert first.status_code == 200
    assert first.data["revoked"] is True
    assert first.data["already_revoked"] is False
    assert not Session.objects.filter(session_key=session_key).exists()

    second = session_workspace["client"].post(url, {}, format="json")
    assert second.status_code == 200
    assert second.data["revoked"] is True
    assert second.data["already_revoked"] is True

    events = AuditEvent.objects.filter(
        tenant=session_workspace["tenant"], event_type="privacy.session.revoked"
    )
    assert events.count() == 1
    event = events.first()
    masked = event.metadata["session_key_masked"]
    assert masked != session_key
    assert "********" in masked
    assert session_key not in event.resource_id
    assert session_key not in json.dumps(event.metadata)


@pytest.mark.django_db
def test_revoking_other_users_session_returns_404(session_workspace):
    other = get_user_model().objects.create_user(
        username="ps-other-owner", password="test-password"
    )
    other_key = _create_session(other)

    response = session_workspace["client"].post(
        _revoke_url(session_workspace["tenant"], other_key), {}, format="json"
    )
    assert response.status_code == 404
    assert response.data["error"]["code"] == "NOT_FOUND"
    assert "detail" not in response.data
    assert Session.objects.filter(session_key=other_key).exists()


@pytest.mark.django_db
def test_revoke_as_non_member_returns_404(session_workspace):
    outsider = get_user_model().objects.create_user(
        username="ps-outsider", password="test-password"
    )
    outsider_tenant = Tenant.objects.create(
        name="PS Outsider Tenant", slug="ps-outsider-tenant"
    )
    TenantMembership.objects.create(
        tenant=outsider_tenant, user=outsider, role=TenantMembership.Role.OWNER
    )
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    other_key = _create_session(outsider)
    response = outsider_client.post(
        _revoke_url(session_workspace["tenant"], other_key), {}, format="json"
    )
    assert response.status_code == 404
    assert response.data["error"]["code"] == "NOT_FOUND"


@pytest.mark.django_db
def test_session_list_requires_authentication(session_workspace):
    anonymous = APIClient()
    response = anonymous.get(_list_url(session_workspace["tenant"]))
    assert response.status_code == 401
    assert response.data["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "detail" not in response.data


@pytest.mark.django_db
def test_session_revoke_requires_authentication(session_workspace):
    session_key = _create_session(session_workspace["user"])
    anonymous = APIClient()
    response = anonymous.post(
        _revoke_url(session_workspace["tenant"], session_key), {}, format="json"
    )
    assert response.status_code == 401
    assert response.data["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "detail" not in response.data
    assert Session.objects.filter(session_key=session_key).exists()
