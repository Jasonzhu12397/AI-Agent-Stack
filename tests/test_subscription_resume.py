"""A scheduled cancellation must be undoable.

Without a resume path, ``cancel at period end`` is a one-way switch: an
accidental click could only be undone by buying another period. These tests
pin the resume endpoint, its audit trail, and its state guards.
"""

import pytest
from core.billing_models import Plan, Subscription
from core.models import Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def resume_setup(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="resume-owner")
    other = user_model.objects.create_user(username="resume-member")
    tenant = Tenant.objects.create(name="Resume", slug="resume")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=other, role="member")
    plan = Plan.objects.create(
        code="resume-monthly",
        name="Resume",
        interval="monthly",
        price_cents=9900,
        token_quota_monthly=10000,
        features=[],
    )
    subscription = Subscription.objects.create(
        tenant=tenant,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        cancel_at_period_end=True,
        current_period_start=timezone.now(),
        current_period_end=timezone.now() + timezone.timedelta(days=20),
    )
    return owner, other, tenant, subscription


def _url(tenant_id):
    return f"/api/v1/tenants/{tenant_id}/billing/subscription/resume/"


@pytest.mark.django_db
def test_resume_clears_the_scheduled_cancel_and_audits(resume_setup):
    from core.audit import AuditEvent

    owner, _other, tenant, subscription = resume_setup
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_url(tenant.id))

    assert response.status_code == 200
    body = response.data["subscription"]
    assert body["cancel_at_period_end"] is False
    assert body["status"] == Subscription.Status.ACTIVE
    subscription.refresh_from_db()
    assert subscription.cancel_at_period_end is False
    assert AuditEvent.objects.filter(
        event_type="billing.subscription.resume_scheduled_cancel", tenant=tenant
    ).exists()


@pytest.mark.django_db
def test_resume_rejects_a_subscription_that_was_never_scheduled(resume_setup):
    owner, _other, tenant, subscription = resume_setup
    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["cancel_at_period_end"])
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_url(tenant.id))

    assert response.status_code == 409
    assert response.data["error"]["code"] == "SUBSCRIPTION_NOT_SCHEDULED_FOR_CANCEL"


@pytest.mark.django_db
def test_resume_rejects_a_terminal_subscription(resume_setup):
    owner, _other, tenant, subscription = resume_setup
    subscription.transition(Subscription.Status.CANCELLED)
    subscription.save(update_fields=["status", "updated_at"])
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(_url(tenant.id))

    assert response.status_code == 409
    assert response.data["error"]["code"] == "SUBSCRIPTION_INVALID_TRANSITION"


@pytest.mark.django_db
def test_resume_requires_a_tenant_admin(resume_setup):
    _owner, member, tenant, _subscription = resume_setup
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.post(_url(tenant.id))

    assert response.status_code == 403
    assert response.data["error"]["code"]
