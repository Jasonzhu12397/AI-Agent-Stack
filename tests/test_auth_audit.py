import pytest
from core.audit import record_audit_event, verify_audit_chain, verify_global_audit_chain
from core.models import AuditEvent, Tenant
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

PASSWORD = "strong-test-password"


def _global_events():
    return AuditEvent.objects.filter(tenant__isnull=True).order_by("created_at", "id")


@pytest.mark.django_db
def test_login_and_logout_events_land_on_the_global_chain():
    user = get_user_model().objects.create_user(
        username="auth-audit-user",
        password=PASSWORD,
    )
    client = APIClient()
    login = client.post(
        "/api/v1/auth/login/",
        {"username": "auth-audit-user", "password": PASSWORD},
        format="json",
    )
    assert login.status_code == 200
    logout = client.post("/api/v1/auth/logout/", {}, format="json")
    assert logout.status_code == 204

    events = list(_global_events())
    assert [event.event_type for event in events] == [
        "auth.login.succeeded",
        "auth.logout.succeeded",
    ]
    assert all(event.tenant_id is None for event in events)
    assert all(event.project_id is None for event in events)
    assert all(event.resource_type == "session" for event in events)
    assert all(str(event.actor_id) == str(user.id) for event in events)
    assert events[0].event_hash == events[1].previous_hash
    assert verify_global_audit_chain() == {
        "valid": True,
        "verified_count": 2,
        "invalid_event_id": None,
        "head_hash": events[1].event_hash,
    }


@pytest.mark.django_db
def test_failed_login_records_a_denied_event_without_identifying_the_account():
    get_user_model().objects.create_user(username="auth-audit-victim", password=PASSWORD)
    client = APIClient()
    response = client.post(
        "/api/v1/auth/login/",
        {"username": "auth-audit-victim", "password": "wrong-password"},
        format="json",
    )
    assert response.status_code == 400

    event = _global_events().get()
    assert event.event_type == "auth.login.denied"
    assert event.outcome == AuditEvent.Outcome.DENIED
    assert event.tenant_id is None
    assert event.actor_id is None
    assert event.metadata == {"reason": "invalid_credentials"}
    assert "auth-audit-victim" not in str(event.metadata)


@pytest.mark.django_db
def test_malformed_login_payload_is_denied_before_authentication():
    client = APIClient()
    response = client.post(
        "/api/v1/auth/login/",
        {"username": "auth-audit-user"},
        format="json",
    )
    assert response.status_code == 400

    event = _global_events().get()
    assert event.event_type == "auth.login.denied"
    assert event.metadata == {"reason": "invalid_payload"}


@pytest.mark.django_db
def test_authentication_events_stay_off_tenant_chains():
    tenant = Tenant.objects.create(name="Auth tenant", slug="auth-tenant")
    record_audit_event(
        None,
        tenant,
        "tenant.created",
        "tenant",
        str(tenant.id),
    )
    client = APIClient()
    client.post(
        "/api/v1/auth/login/",
        {"username": "unknown", "password": "wrong"},
        format="json",
    )

    tenant_chain = verify_audit_chain(tenant)
    global_chain = verify_global_audit_chain()
    assert tenant_chain["valid"] is True
    assert tenant_chain["verified_count"] == 1
    assert global_chain["valid"] is True
    assert global_chain["verified_count"] == 1
    assert AuditEvent.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_global_chain_detects_tampering():
    client = APIClient()
    client.post(
        "/api/v1/auth/login/",
        {"username": "unknown", "password": "wrong"},
        format="json",
    )
    client.post(
        "/api/v1/auth/login/",
        {"username": "unknown", "password": "wrong"},
        format="json",
    )
    first = _global_events().first()
    AuditEvent.objects.filter(pk=first.pk).update(metadata={"reason": "tampered"})

    verification = verify_global_audit_chain()
    assert verification["valid"] is False
    assert verification["invalid_event_id"] == str(first.id)
