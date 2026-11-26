"""Chat workspace contract: composer settings, attachments and scheduled turns."""

from datetime import timedelta

import pytest
from core.chat_models import AgentChatAttachment, AgentChatThread
from core.models import (
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.scheduled_models import ScheduledChatJob
from core.scheduled_service import dispatch_due_jobs, next_occurrence
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def workspace(db, tmp_path):
    user = get_user_model().objects.create_user(username="workspace-owner")
    tenant = Tenant.objects.create(name="Workspace tenant", slug="workspace-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Workspace project", slug="ws-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.VIEWER
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Workspace fake provider",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "scheduled reply"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="ws-fake",
        display_name="WS Fake",
        capabilities=["chat"],
        max_output_tokens=1024,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {
        "user": user,
        "tenant": tenant,
        "project": project,
        "model": model,
        "client": client,
        "media": str(tmp_path),
    }


def make_thread(workspace, client=None, title="Composer settings"):
    client = client or workspace["client"]
    response = client.post(
        f"/api/v1/tenants/{workspace['tenant'].id}/projects/{workspace['project'].id}"
        "/agent-chat/threads/",
        {"title": title, "model_config_id": str(workspace["model"].id)},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.mark.django_db
def test_thread_settings_are_validated_and_stored(workspace):
    client = workspace["client"]
    thread = make_thread(workspace)
    url = f"/api/v1/agent-chat/threads/{thread['id']}/"

    response = client.patch(
        url,
        {
            "execution_target": "local",
            "effort": "low",
            "goal": "Turn two spare hosts into workers",
            "plan_first": True,
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["execution_target"] == "local"
    assert body["effort"] == "low"
    assert body["goal"] == "Turn two spare hosts into workers"
    assert body["plan_first"] is True
    stored = AgentChatThread.objects.get(pk=thread["id"])
    assert stored.goal.startswith("Turn two spare hosts")

    bad_target = client.patch(url, {"execution_target": "edge"}, format="json")
    assert bad_target.status_code == 400
    assert bad_target.json()["error"]["code"] == "INVALID_EXECUTION_TARGET"
    bad_effort = client.patch(url, {"effort": "high"}, format="json")
    assert bad_effort.status_code == 400
    assert bad_effort.json()["error"]["code"] == "INVALID_EFFORT"


@pytest.mark.django_db
def test_turn_records_the_effort_and_execution_target_used(workspace):
    client = workspace["client"]
    thread = make_thread(workspace)
    client.patch(
        f"/api/v1/agent-chat/threads/{thread['id']}/",
        {"effort": "low", "execution_target": "local"},
        format="json",
    )
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "status?"},
        format="json",
    )
    assert response.status_code == 201, response.content
    usage = response.json()["assistant"]["usage"]
    assert usage["effort"] == "low"
    assert usage["execution_target"] == "local"


@override_settings(MEDIA_ROOT="/tmp/chat-ws-media")
@pytest.mark.django_db
def test_attachment_is_stored_named_in_the_turn_and_downloadable(workspace):
    client = workspace["client"]
    thread = make_thread(workspace)
    upload = SimpleUploadedFile(
        "fleet.csv", b"host,role\nbm-01,worker\n", content_type="text/csv"
    )
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/attachments/",
        {"file": upload},
        format="multipart",
    )
    assert response.status_code == 201, response.content
    attachment = response.json()
    assert attachment["filename"] == "fleet.csv"
    assert attachment["size_bytes"] == len(b"host,role\nbm-01,worker\n")

    turn = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "Read the inventory", "context": {"attachments": [attachment["id"]]}},
        format="json",
    )
    assert turn.status_code == 201, turn.content
    assert turn.json()["user"]["context"]["attachments"] == ["fleet.csv"]

    download = client.get(f"/api/v1/agent-chat/attachments/{attachment['id']}/")
    assert download.status_code == 200
    assert b"bm-01" in b"".join(download.streaming_content)

    # An attachment uploaded to another thread cannot be smuggled into this one.
    other = make_thread(workspace, title="Other thread")
    stray = client.post(
        f"/api/v1/agent-chat/threads/{other['id']}/attachments/",
        {"file": SimpleUploadedFile("other.txt", b"x")},
        format="multipart",
    ).json()
    rejected = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "wrong file", "context": {"attachments": [stray["id"]]}},
        format="json",
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "INVALID_CONTEXT"
    assert AgentChatAttachment.objects.filter(thread_id=thread["id"]).count() == 1


@pytest.mark.django_db
def test_scheduled_job_creation_validates_and_schedules_the_next_slot(workspace):
    client = workspace["client"]
    url = (
        f"/api/v1/tenants/{workspace['tenant'].id}/projects/{workspace['project'].id}"
        "/scheduled-jobs/"
    )
    response = client.post(
        url,
        {
            "name": "Morning fleet report",
            "prompt": "Summarise pending enrollments",
            "cadence": "daily",
            "hour": 8,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    job = response.json()
    assert job["cadence"] == "daily"
    assert job["enabled"] is True
    assert job["hour"] == 8

    missing = client.post(url, {"name": "no prompt"}, format="json")
    assert missing.status_code == 400
    bad_hour = client.post(
        url, {"name": "x", "prompt": "y", "cadence": "daily", "hour": 24}, format="json"
    )
    assert bad_hour.status_code == 400


@pytest.mark.django_db
def test_run_now_opens_a_thread_and_appends_the_turn(workspace):
    client = workspace["client"]
    base = (
        f"/api/v1/tenants/{workspace['tenant'].id}/projects/{workspace['project'].id}"
        "/scheduled-jobs/"
    )
    job = client.post(
        base,
        {"name": "Nightly check", "prompt": "Any worker down?", "cadence": "hourly"},
        format="json",
    ).json()
    response = client.post(f"{base}{job['id']}/run-now/", {}, format="json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["thread_id"]
    assert body["last_run_at"]
    stored = ScheduledChatJob.objects.get(pk=job["id"])
    messages = stored.thread.messages.order_by("created_at")
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].content == "scheduled reply"


@pytest.mark.django_db
def test_due_jobs_are_claimed_once_and_advanced(workspace):
    client = workspace["client"]
    base = (
        f"/api/v1/tenants/{workspace['tenant'].id}/projects/{workspace['project'].id}"
        "/scheduled-jobs/"
    )
    job = client.post(
        base, {"name": "Due now", "prompt": "report", "cadence": "hourly"}, format="json"
    ).json()
    ScheduledChatJob.objects.filter(pk=job["id"]).update(
        next_run_at=timezone.now() - timedelta(minutes=5)
    )
    ran = dispatch_due_jobs(limit=2)
    assert ran == 1
    stored = ScheduledChatJob.objects.get(pk=job["id"])
    assert stored.last_run_at is not None
    assert stored.next_run_at > timezone.now()
    # A second pass has nothing due: the slot was claimed exactly once.
    assert dispatch_due_jobs(limit=2) == 0


@pytest.mark.django_db
def test_paused_job_is_never_dispatched(workspace):
    client = workspace["client"]
    base = (
        f"/api/v1/tenants/{workspace['tenant'].id}/projects/{workspace['project'].id}"
        "/scheduled-jobs/"
    )
    job = client.post(
        base, {"name": "Paused", "prompt": "noop", "cadence": "daily"}, format="json"
    ).json()
    paused = client.patch(f"{base}{job['id']}/", {"enabled": False}, format="json")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    ScheduledChatJob.objects.filter(pk=job["id"]).update(
        next_run_at=timezone.now() - timedelta(hours=1)
    )
    assert dispatch_due_jobs(limit=5) == 0


def test_next_occurrence_follows_the_cadence():
    job = ScheduledChatJob(cadence=ScheduledChatJob.Cadence.DAILY, hour=7)
    anchor = timezone.localtime(timezone.now()).replace(hour=6, minute=30, second=0, microsecond=0)
    upcoming = next_occurrence(job, anchor)
    assert upcoming.hour == 7
    after = anchor.replace(hour=9)
    assert next_occurrence(job, after).day != after.day or next_occurrence(job, after).hour == 7
    weekly = ScheduledChatJob(cadence=ScheduledChatJob.Cadence.WEEKLY, hour=9, weekday=0)
    slot = next_occurrence(weekly, anchor)
    assert slot.weekday() == 0
