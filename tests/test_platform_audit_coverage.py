"""Task, token budget, tenant and project writes must record audit events.

These are the platform-level provisioners: they create the scopes (tenant,
project) that every other audit event is keyed to, they move work into the
queue, and they change the spend ceiling the platform enforces at runtime. A
missing audit row here means a change to who can spend how much is invisible.
"""

import pytest
from core.models import (
    AuditEvent,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    TenantTokenBudget,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def platform_admin(db):
    user = get_user_model().objects.create_user(username="platform-admin")
    tenant = Tenant.objects.create(name="Platform tenant", slug="platform-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(
        tenant=tenant,
        name="Platform project",
        slug="platform-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.EDITOR,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "project": project, "client": client}


def _events(tenant, event_type):
    return AuditEvent.objects.filter(tenant=tenant, event_type=event_type)


@pytest.mark.django_db
def test_tenant_creation_is_audited_against_the_new_tenant(db):
    user = get_user_model().objects.create_user(username="tenant-creator")
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        "/api/v1/tenants/",
        {"name": "Fresh tenant", "slug": "fresh-tenant"},
        format="json",
    )
    assert created.status_code == 201
    tenant_id = created.data["id"]

    event = AuditEvent.objects.get(event_type="tenant.created", resource_id=tenant_id)
    assert str(event.tenant_id) == tenant_id
    assert event.resource_type == "tenant"
    assert event.metadata["slug"] == "fresh-tenant"
    assert event.actor_id == user.pk


@pytest.mark.django_db
def test_project_creation_is_audited(platform_admin):
    created = platform_admin["client"].post(
        f"/api/v1/tenants/{platform_admin['tenant'].id}/projects/",
        {"name": "Second project", "slug": "second-project"},
        format="json",
    )
    assert created.status_code == 201

    event = _events(platform_admin["tenant"], "project.created").get()
    assert event.resource_type == "project"
    assert event.resource_id == created.data["id"]
    assert str(event.project_id) == created.data["id"]
    assert event.metadata["slug"] == "second-project"


@pytest.mark.django_db
def test_task_creation_is_audited_once_per_request(platform_admin):
    client = platform_admin["client"]
    url = (
        f"/api/v1/tenants/{platform_admin['tenant'].id}"
        f"/projects/{platform_admin['project'].id}/tasks/"
    )
    body = {"task_type": "demo.echo", "payload": {"message": "hello"}}

    first = client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="audit-task-key")
    assert first.status_code == 202
    created_event = _events(platform_admin["tenant"], "task.created").get()
    assert created_event.resource_type == "task"
    assert created_event.resource_id == first.data["id"]
    assert created_event.metadata["task_type"] == "demo.echo"
    assert created_event.metadata["deduplicated"] is False

    repeated = client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="audit-task-key")
    assert repeated.status_code == 200
    replay_event = _events(platform_admin["tenant"], "task.created").latest("created_at")
    assert replay_event.resource_id == first.data["id"]
    assert replay_event.metadata["deduplicated"] is True


@pytest.mark.django_db
def test_task_cancellation_is_audited_with_the_previous_status(platform_admin):
    client = platform_admin["client"]
    task = Task.objects.create(
        tenant=platform_admin["tenant"],
        project=platform_admin["project"],
        created_by=platform_admin["user"],
        task_type="demo.echo",
        payload={"message": "cancel me"},
        idempotency_key="audit-cancel-key",
        available_at=timezone.now(),
    )

    cancelled = client.post(f"/api/v1/tasks/{task.id}/cancel/", {}, format="json")
    assert cancelled.status_code == 202

    event = _events(platform_admin["tenant"], "task.cancelled").get()
    assert event.resource_type == "task"
    assert event.resource_id == str(task.id)
    assert event.metadata["previous_status"] == Task.Status.QUEUED
    assert event.metadata["status"] == Task.Status.CANCELLED
    assert event.metadata["cancelled_approvals"] == 0


@pytest.mark.django_db
def test_token_budget_update_is_audited(platform_admin):
    url = f"/api/v1/tenants/{platform_admin['tenant'].id}/token-budget/"
    updated = platform_admin["client"].put(
        url,
        {"daily_limit": 5000, "monthly_limit": 90000},
        format="json",
    )
    assert updated.status_code == 200

    budget = TenantTokenBudget.objects.get(tenant=platform_admin["tenant"])
    event = _events(platform_admin["tenant"], "token_budget.updated").get()
    assert event.resource_type == "tenant_token_budget"
    assert event.resource_id == str(budget.id)
    assert event.metadata["daily_limit"] == 5000
    assert event.metadata["monthly_limit"] == 90000


@pytest.mark.django_db
def test_token_budget_read_does_not_create_an_audit_event(platform_admin):
    url = f"/api/v1/tenants/{platform_admin['tenant'].id}/token-budget/"
    response = platform_admin["client"].get(url)
    assert response.status_code == 200
    assert not _events(platform_admin["tenant"], "token_budget.updated").exists()
