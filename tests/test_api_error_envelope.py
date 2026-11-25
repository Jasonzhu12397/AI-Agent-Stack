"""Real-HTTP contract tests for the shared settings-center error envelope.

The frozen contract requires every error response to be shaped as::

    {"error": {"code": "UPPER_SNAKE_CODE", "message": "...", "details": {...}}}

These tests drive one endpoint per area (billing, connectors, data-controls,
archive, privacy) over ``APIClient`` and assert:

* an anonymous caller receives strict 401 with the envelope
  (``AUTHENTICATION_REQUIRED``);
* an authenticated non-member of the scope receives 404 with the envelope
  (``NOT_FOUND``), without disclosing whether the resource exists;
* the DRF default ``{"detail": ...}`` shape never appears as a top-level key;
* an existing business error code (``PAYMENT_PROVIDER_NOT_CONFIGURED``) keeps
  its envelope shape and status.
"""

import pytest
from core.billing_models import Plan
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def envelope_workspace(db):
    user_model = get_user_model()
    insider = user_model.objects.create_user(username="env-insider")
    outsider = user_model.objects.create_user(username="env-outsider")
    tenant = Tenant.objects.create(name="Env tenant", slug="env-tenant")
    foreign = Tenant.objects.create(name="Env foreign", slug="env-foreign")
    TenantMembership.objects.create(tenant=tenant, user=insider, role="owner")
    TenantMembership.objects.create(tenant=foreign, user=outsider, role="owner")
    project = Project.objects.create(
        tenant=tenant, name="Env project", slug="env-project"
    )
    ProjectMembership.objects.create(project=project, user=insider, role="admin")
    plan = Plan.objects.create(
        code="env-pro",
        name="Env Pro",
        interval="monthly",
        price_cents=9900,
        token_quota_daily=10_000,
        token_quota_monthly=50_000,
        features=["chat"],
    )
    return {
        "insider": insider,
        "outsider": outsider,
        "tenant": tenant,
        "foreign": foreign,
        "project": project,
        "plan": plan,
    }


def _tenant_path(tenant_id: str, path: str) -> str:
    return f"/api/v1/tenants/{tenant_id}/{path}/"


# One endpoint per settings-center area. ``scope`` drives the non-member check.
ENDPOINTS = [
    {
        "label": "billing",
        "scope": "tenant",
        "url": lambda w: _tenant_path(w["tenant"].id, "billing/plans"),
    },
    {
        "label": "agent-plugins",
        "scope": "tenant",
        "url": lambda w: (
            f"/api/v1/tenants/{w['tenant'].id}/projects/{w['project'].id}"
            "/agent-plugin-installations/"
        ),
    },
    {
        "label": "data-controls",
        "scope": "tenant",
        "url": lambda w: _tenant_path(w["tenant"].id, "data-controls/exports"),
    },
    {
        "label": "archive",
        "scope": "project",
        "url": lambda w: (
            f"/api/v1/tenants/{w['tenant'].id}/projects/{w['project'].id}/"
            "archive-items/"
        ),
    },
    {
        "label": "privacy",
        "scope": "tenant",
        "url": lambda w: _tenant_path(w["tenant"].id, "privacy/grants"),
    },
]


def _endpoint(label: str) -> dict:
    return next(entry for entry in ENDPOINTS if entry["label"] == label)


def _assert_envelope(response, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.data
    # The DRF default shape must not leak as a top-level key.
    assert "detail" not in response.data
    assert "error" in response.data, response.data
    error = response.data["error"]
    assert {"code", "message", "details"} <= set(error)
    assert error["code"] == code
    assert isinstance(error["details"], dict)


@pytest.mark.django_db
@pytest.mark.parametrize("entry", ENDPOINTS, ids=lambda entry: entry["label"])
def test_anonymous_rejected_with_401_envelope(entry, envelope_workspace):
    client = APIClient()
    response = client.get(entry["url"](envelope_workspace))
    _assert_envelope(response, status_code=401, code="AUTHENTICATION_REQUIRED")


@pytest.mark.django_db
@pytest.mark.parametrize("entry", ENDPOINTS, ids=lambda entry: entry["label"])
def test_non_member_rejected_with_404_envelope(entry, envelope_workspace):
    client = APIClient()
    client.force_authenticate(user=envelope_workspace["outsider"])
    response = client.get(entry["url"](envelope_workspace))
    _assert_envelope(response, status_code=404, code="NOT_FOUND")


@pytest.mark.django_db
def test_business_error_code_shape_preserved(envelope_workspace):
    """A configured business error keeps its code, status and envelope shape."""

    client = APIClient()
    client.force_authenticate(user=envelope_workspace["insider"])
    response = client.post(
        _tenant_path(envelope_workspace["tenant"].id, "billing/orders"),
        {
            "plan_code": envelope_workspace["plan"].code,
            "channel": "wechat",
            "idempotency_key": "env-no-provider-1",
        },
        format="json",
    )

    _assert_envelope(
        response,
        status_code=409,
        code="PAYMENT_PROVIDER_NOT_CONFIGURED",
    )
