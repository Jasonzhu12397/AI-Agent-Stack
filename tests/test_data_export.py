import json
import uuid
from pathlib import Path

import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def export_workspace(db):
    user = get_user_model().objects.create_user(
        username="dc-export-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="DC Export Tenant", slug="dc-export-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="DC Export Project", slug="dc-export-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "project": project, "client": client}


def _seed_sensitive_task(workspace, marker_prefix="WHISPER"):
    task = Task.objects.create(
        tenant=workspace["tenant"],
        project=workspace["project"],
        created_by=workspace["user"],
        task_type="chat",
        payload={"prompt": f"{marker_prefix}_PROMPT_XYZ", "api_key": f"{marker_prefix}_KEY_999"},
        result={"answer": f"{marker_prefix}_RESULT_ABC"},
        error={"message": f"{marker_prefix}_ERROR_DEF"},
        status=Task.Status.SUCCEEDED,
        idempotency_key=f"task-{uuid.uuid4().hex[:12]}",
        available_at=timezone.now(),
    )
    agent = AgentDefinition.objects.create(
        tenant=workspace["tenant"],
        project=workspace["project"],
        created_by=workspace["user"],
        name="Export agent",
        slug="export-agent",
    )
    version = AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot={},
        created_by=workspace["user"],
    )
    AgentRun.objects.create(
        task=task,
        agent_version=version,
        input_text=f"{marker_prefix}_INPUT_ABC",
        output_text=f"{marker_prefix}_OUTPUT_DEF",
        provider_name="test-provider",
        model_name="test-model",
    )
    return task


@pytest.mark.django_db
def test_data_export_idempotent_job(export_workspace):
    _seed_sensitive_task(export_workspace)
    url = f"/api/v1/tenants/{export_workspace['tenant'].id}/data-controls/exports/"
    body = {
        "idempotency_key": "export-job-1",
        "scope": {"kinds": ["tasks", "runs", "audit_events"]},
    }

    first = export_workspace["client"].post(url, body, format="json")
    assert first.status_code == 201
    assert first.data["status"] == "succeeded"
    assert first.data["row_count"] >= 1
    assert first.data["file_ref"]

    second = export_workspace["client"].post(url, body, format="json")
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert second.data["file_ref"] == first.data["file_ref"]


@pytest.mark.django_db
def test_data_export_whitelist_excludes_secrets(export_workspace):
    task = _seed_sensitive_task(export_workspace)
    url = f"/api/v1/tenants/{export_workspace['tenant'].id}/data-controls/exports/"
    response = export_workspace["client"].post(
        url,
        {"idempotency_key": "export-whitelist", "scope": {"kinds": ["tasks", "runs"]}},
        format="json",
    )
    assert response.status_code == 201
    file_ref = response.data["file_ref"]
    assert file_ref

    export_path = Path(file_ref)
    assert export_path.exists()
    raw = export_path.read_text(encoding="utf-8")
    for marker in (
        "WHISPER_PROMPT_XYZ",
        "WHISPER_KEY_999",
        "WHISPER_RESULT_ABC",
        "WHISPER_ERROR_DEF",
        "WHISPER_INPUT_ABC",
        "WHISPER_OUTPUT_DEF",
    ):
        assert marker not in raw, f"sensitive content leaked into export: {marker}"

    payload = json.loads(raw)
    tasks = payload["tasks"]
    assert any(item["id"] == str(task.id) for item in tasks)
    sample = next(item for item in tasks if item["id"] == str(task.id))
    assert sample["status"] == Task.Status.SUCCEEDED
    assert "payload" not in sample
    assert "result" not in sample
    assert "error" not in sample


@pytest.mark.django_db
def test_data_export_not_readable_before_success(export_workspace):
    from core.data_controls_models import DataExportJob

    job = DataExportJob.objects.create(
        tenant=export_workspace["tenant"],
        project=export_workspace["project"],
        requested_by=export_workspace["user"],
        scope={"kinds": ["tasks"]},
        status=DataExportJob.Status.PENDING,
        idempotency_key="pending-job",
    )

    detail = export_workspace["client"].get(
        f"/api/v1/tenants/{export_workspace['tenant'].id}/data-controls/exports/{job.id}/"
    )
    assert detail.status_code == 409
    assert detail.data["error"]["code"] == "EXPORT_NOT_READY"
    assert "file_ref" not in detail.data or not detail.data.get("file_ref")

    listing = export_workspace["client"].get(
        f"/api/v1/tenants/{export_workspace['tenant'].id}/data-controls/exports/"
    )
    assert listing.status_code == 200
    pending_entry = next(item for item in listing.data["results"] if item["id"] == str(job.id))
    assert pending_entry["status"] == "pending"
    assert not pending_entry.get("file_ref")
