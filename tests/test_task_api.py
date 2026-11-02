import pytest
from core.models import Project, ProjectMembership, Task, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def task_users(db):
    user_model = get_user_model()

    def create(username):
        return user_model.objects.create_user(username=username, password="test-password")

    return create


@pytest.mark.django_db
def test_task_creation_is_async_and_idempotent(task_users):
    editor = task_users("task-editor")
    tenant = Tenant.objects.create(name="Task tenant", slug="task-tenant")
    TenantMembership.objects.create(tenant=tenant, user=editor, role="member")
    project = Project.objects.create(tenant=tenant, name="Task project", slug="task-project")
    ProjectMembership.objects.create(project=project, user=editor, role="editor")
    client = APIClient()
    client.force_authenticate(user=editor)
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/tasks/"
    body = {"task_type": "demo.echo", "payload": {"message": "hello"}}

    created = client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="task-key-001")
    repeated = client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="task-key-001")

    assert created.status_code == 202
    assert created.data["status"] == Task.Status.QUEUED
    assert repeated.status_code == 200
    assert repeated.data["id"] == created.data["id"]
    assert Task.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_idempotency_key_cannot_be_reused_for_different_payload(task_users):
    editor = task_users("task-idempotency-editor")
    tenant = Tenant.objects.create(name="Idempotency", slug="idempotency")
    TenantMembership.objects.create(tenant=tenant, user=editor, role="member")
    project = Project.objects.create(tenant=tenant, name="Project", slug="idempotency-project")
    ProjectMembership.objects.create(project=project, user=editor, role="editor")
    client = APIClient()
    client.force_authenticate(user=editor)
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/tasks/"

    first = client.post(
        url,
        {"task_type": "demo.echo", "payload": {"value": 1}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="duplicate-key",
    )
    second = client.post(
        url,
        {"task_type": "demo.echo", "payload": {"value": 2}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="duplicate-key",
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert Task.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_viewer_cannot_create_task_and_foreign_tenant_cannot_read(task_users):
    viewer = task_users("task-viewer")
    outsider = task_users("task-outsider")
    tenant = Tenant.objects.create(name="Task permissions", slug="task-permissions")
    foreign = Tenant.objects.create(name="Foreign task", slug="foreign-task")
    TenantMembership.objects.create(tenant=tenant, user=viewer, role="member")
    TenantMembership.objects.create(tenant=foreign, user=outsider, role="owner")
    project = Project.objects.create(tenant=tenant, name="Read only", slug="read-only")
    ProjectMembership.objects.create(project=project, user=viewer, role="viewer")
    client = APIClient()
    client.force_authenticate(user=viewer)
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/tasks/"

    denied = client.post(
        url,
        {"task_type": "demo.echo", "payload": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="viewer-denied",
    )

    assert denied.status_code == 403
    assert Task.objects.count() == 0


@pytest.mark.django_db
def test_queued_task_can_be_cancelled_by_project_member(task_users):
    editor = task_users("task-cancel-editor")
    tenant = Tenant.objects.create(name="Cancel tenant", slug="cancel-tenant")
    TenantMembership.objects.create(tenant=tenant, user=editor, role="member")
    project = Project.objects.create(tenant=tenant, name="Cancel project", slug="cancel-project")
    ProjectMembership.objects.create(project=project, user=editor, role="editor")
    task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=editor,
        task_type="demo.echo",
        payload={},
        idempotency_key="cancel-task-001",
        available_at=timezone.now(),
    )
    client = APIClient()
    client.force_authenticate(user=editor)

    response = client.post(f"/api/v1/tasks/{task.id}/cancel/", {}, format="json")

    assert response.status_code == 202
    task.refresh_from_db()
    assert task.status == Task.Status.CANCELLED
    assert task.finished_at is not None


@pytest.mark.django_db
def test_running_task_cancel_is_cooperative(task_users):
    editor = task_users("running-cancel-editor")
    tenant = Tenant.objects.create(name="Running cancel", slug="running-cancel")
    TenantMembership.objects.create(tenant=tenant, user=editor, role="member")
    project = Project.objects.create(tenant=tenant, name="Running project", slug="running-project")
    ProjectMembership.objects.create(project=project, user=editor, role="editor")
    task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=editor,
        task_type="demo.sleep",
        payload={"seconds": 5},
        idempotency_key="running-cancel-001",
        status=Task.Status.RUNNING,
        available_at=timezone.now(),
        lease_owner="worker",
        lease_token="00000000-0000-0000-0000-000000000001",
        lease_expires_at=timezone.now() + timezone.timedelta(seconds=30),
    )
    client = APIClient()
    client.force_authenticate(user=editor)

    response = client.post(f"/api/v1/tasks/{task.id}/cancel/", {}, format="json")

    assert response.status_code == 202
    task.refresh_from_db()
    assert task.status == Task.Status.CANCEL_REQUESTED
    assert task.cancel_requested_at is not None
