import pytest
from core.audit import record_audit_event
from core.models import AuditEvent, Tenant
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

GLOBAL_LIST = "/api/v1/audit-events/global/"
GLOBAL_VERIFY = "/api/v1/audit-events/global/verify/"


def _staff_client():
    user = get_user_model().objects.create_user(username="platform-admin", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _member_client():
    user = get_user_model().objects.create_user(username="plain-member")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _global_events():
    return AuditEvent.objects.filter(tenant__isnull=True).order_by("created_at", "id")


@pytest.mark.django_db
def test_global_chain_requires_platform_administrator():
    client = _member_client()
    assert client.get(GLOBAL_LIST).status_code == 403
    assert client.get(GLOBAL_VERIFY).status_code == 403
    assert client.get(GLOBAL_LIST).json() == {
        "detail": "Platform administrator permission required to view the global audit chain."
    }


@pytest.mark.django_db
def test_global_chain_lists_authentication_events_only():
    record_audit_event(None, None, "auth.login.succeeded", "session", metadata={"reason": "ok"})
    record_audit_event(None, None, "auth.login.denied", "session", metadata={"reason": "denied"})
    tenant = Tenant.objects.create(name="Audit tenant", slug="audit-tenant")
    record_audit_event(None, tenant, "tenant.created", "tenant", tenant.id)

    response = _staff_client().get(GLOBAL_LIST)
    body = response.json()

    assert response.status_code == 200
    assert body["count"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["event_type"] for item in body["results"]] == [
        "auth.login.denied",
        "auth.login.succeeded",
    ]
    assert all(item["tenant_id"] is None for item in body["results"])
    assert all(item["project_id"] is None for item in body["results"])


@pytest.mark.django_db
def test_global_chain_validates_pagination():
    client = _staff_client()
    assert client.get(GLOBAL_LIST, {"limit": 0}).status_code == 400
    assert client.get(GLOBAL_LIST, {"limit": 500}).status_code == 400
    assert client.get(GLOBAL_LIST, {"offset": -1}).status_code == 400
    assert client.get(GLOBAL_LIST, {"limit": "abc"}).status_code == 400

    record_audit_event(None, None, "auth.logout.succeeded", "session")
    response = client.get(GLOBAL_LIST, {"limit": 1, "offset": 0})
    assert response.status_code == 200
    assert response.json()["count"] == 1


@pytest.mark.django_db
def test_global_chain_verify_reports_a_valid_chain():
    record_audit_event(None, None, "auth.login.succeeded", "session", metadata={"reason": "ok"})
    record_audit_event(None, None, "auth.logout.succeeded", "session", metadata={"reason": "ok"})

    response = _staff_client().get(GLOBAL_VERIFY)

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "verified_count": 2,
        "invalid_event_id": None,
        "head_hash": _global_events().last().event_hash,
    }


@pytest.mark.django_db
def test_global_chain_verify_detects_tampering():
    record_audit_event(None, None, "auth.login.succeeded", "session", metadata={"reason": "ok"})
    record_audit_event(None, None, "auth.logout.succeeded", "session", metadata={"reason": "ok"})
    first = _global_events().first()
    AuditEvent.objects.filter(pk=first.pk).update(metadata={"reason": "tampered"})

    response = _staff_client().get(GLOBAL_VERIFY)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["verified_count"] == 0
    assert response.json()["invalid_event_id"] == str(first.id)
