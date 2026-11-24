"""Session metadata: real creation/last-activity timestamps instead of nulls.

The account and privacy session lists used to report ``created_at: null`` and
``last_activity: null`` because ``django_session`` has no timestamp columns.
These tests pin the replacement contract: a durable side table, one conditional
UPDATE per request, honest ``unavailable`` for sessions that predate it, and a
revocation trail.
"""

from datetime import timedelta

import pytest
from core.models import Tenant, TenantMembership
from core.session_activity import (
    ACTIVITY_REFRESH_INTERVAL,
    SessionActivity,
    mark_revoked,
    record_login,
)
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone


@pytest.fixture
def activity_setup(db):
    user = get_user_model().objects.create_user(username="activity-user")
    tenant = Tenant.objects.create(name="Activity", slug="activity")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    return user, tenant


@pytest.mark.django_db
def test_login_records_the_session_with_real_timestamps(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)

    key = client.session.session_key
    row = SessionActivity.objects.get(pk=key)
    assert row.user_id == user.pk
    assert row.created_at is not None
    assert row.last_activity is not None
    assert row.revoked_at is None


@pytest.mark.django_db
def test_authenticated_request_refreshes_a_stale_last_activity(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)
    key = client.session.session_key
    old = timezone.now() - timedelta(hours=3)
    SessionActivity.objects.filter(pk=key).update(last_activity=old)

    assert client.get("/api/v1/users/me/").status_code == 200

    row = SessionActivity.objects.get(pk=key)
    assert row.last_activity > old
    assert row.last_activity > timezone.now() - timedelta(minutes=2)


@pytest.mark.django_db
def test_a_burst_of_requests_writes_last_activity_at_most_once(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)
    key = client.session.session_key
    SessionActivity.objects.filter(pk=key).update(
        last_activity=timezone.now() - ACTIVITY_REFRESH_INTERVAL / 2
    )
    before = SessionActivity.objects.get(pk=key).last_activity

    client.get("/api/v1/users/me/")
    client.get("/api/v1/users/me/")

    assert SessionActivity.objects.get(pk=key).last_activity == before


@pytest.mark.django_db
def test_account_sessions_report_metadata_instead_of_null(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)

    response = client.get("/api/v1/users/me/sessions/")

    assert response.status_code == 200
    rows = response.data["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["is_current"] is True
    assert row["created_at"] is not None
    assert row["last_activity"] is not None
    assert row["timestamps_source"] == "session_metadata"


@pytest.mark.django_db
def test_privacy_sessions_report_metadata_and_keep_derivation_as_fallback(
    activity_setup,
):
    user, tenant = activity_setup
    client = Client()
    client.force_login(user)
    meta_key = client.session.session_key
    record_login(session_key=meta_key, user=user)

    # A session that predates the table: insert its payload, no metadata row.
    second = Client()
    second.force_login(user)
    legacy_key = second.session.session_key
    SessionActivity.objects.filter(pk=legacy_key).delete()

    response = client.get(f"/api/v1/tenants/{tenant.id}/privacy/sessions/")
    assert response.status_code == 200
    by_key = {row["session_key_masked"]: row for row in response.data["results"]}
    assert len(by_key) == 2

    masked_meta = next(
        row for row in response.data["results"] if row["created_at_source"] == "session_metadata"
    )
    assert masked_meta["last_activity"] is not None
    assert masked_meta["last_activity_source"] == "session_metadata"

    legacy = next(
        row
        for row in response.data["results"]
        if row["created_at_source"] == "derived_from_expire_date"
    )
    assert legacy["last_activity"] is None
    assert legacy["last_activity_source"] == "unavailable"


@pytest.mark.django_db
def test_revoking_a_session_leaves_a_metadata_trail(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)
    key = client.session.session_key

    assert client.post(f"/api/v1/users/me/sessions/{key}/revoke/").status_code == 200

    row = SessionActivity.objects.get(pk=key)
    assert row.revoked_at is not None


@pytest.mark.django_db
def test_logout_marks_the_session_revoked(activity_setup):
    user, _tenant = activity_setup
    client = Client()
    client.force_login(user)
    key = client.session.session_key

    client.logout()

    assert SessionActivity.objects.get(pk=key).revoked_at is not None


@pytest.mark.django_db
def test_metadata_stores_no_network_identifiers(activity_setup):
    """Privacy: the table must not become a shadow access log."""

    field_names = {field.name for field in SessionActivity._meta.get_fields()}
    assert "ip" not in field_names
    assert "ip_address" not in field_names
    assert "user_agent" not in field_names


@pytest.mark.django_db
def test_record_login_is_idempotent_per_key(activity_setup):
    user, _tenant = activity_setup
    first = record_login(session_key="k1", user=user)
    second = record_login(session_key="k1", user=user)

    assert SessionActivity.objects.filter(pk="k1").count() == 1
    assert first.created_at == second.created_at
    mark_revoked("k1")
    record_login(session_key="k1", user=user)
    assert SessionActivity.objects.get(pk="k1").revoked_at is None
