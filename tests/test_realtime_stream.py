from datetime import timedelta

import pytest
from core.models import Project, ProjectMembership, Task, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(username="stream-user", password="test-password")
    tenant = Tenant.objects.create(name="Stream tenant", slug="stream-tenant")
    project = Project.objects.create(tenant=tenant, name="Stream project", slug="stream-project")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.ADMIN)
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.EDITOR)
    return {"user": user, "tenant": tenant, "project": project}


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _url(workspace):
    return reverse(
        "project-event-stream",
        kwargs={
            "tenant_id": workspace["tenant"].id,
            "project_id": workspace["project"].id,
        },
    )


@pytest.mark.django_db
def test_stream_replays_new_task_events(workspace, settings):
    settings.REALTIME_STREAM_POLL_SECONDS = 0.1
    task = Task.objects.create(
        tenant=workspace["tenant"],
        project=workspace["project"],
        created_by=workspace["user"],
        task_type="demo.echo",
        payload={"text": "stream"},
        idempotency_key="stream-task-1",
        available_at=timezone.now(),
    )
    since = (timezone.now() - timedelta(seconds=2)).isoformat()
    response = _client(workspace["user"]).get(
        _url(workspace),
        {"since": since, "max_events": 1},
        HTTP_ACCEPT="text/event-stream",
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    body = b"".join(response.streaming_content).decode("utf-8")
    assert "event: ready" in body
    assert "event: task" in body
    assert f'"id":"{task.id}"' in body
    assert '"status":"queued"' in body


@pytest.mark.django_db
def test_stream_rejects_an_invalid_limit(workspace):
    response = _client(workspace["user"]).get(_url(workspace), {"max_events": "0"})
    assert response.status_code == 400


@pytest.mark.django_db
def test_stream_is_scoped_to_project_members(workspace):
    outsider = get_user_model().objects.create_user(
        username="stream-outsider",
        password="test-password",
    )
    response = _client(outsider).get(_url(workspace), {"max_events": 1})
    assert response.status_code == 404
