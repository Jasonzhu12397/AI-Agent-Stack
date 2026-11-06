# ruff: noqa: I001
from datetime import timedelta

import pytest
from core.models import (
    PluginDefinition,
    PluginInstallation,
    PluginTool,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    ToolApprovalRequest,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def approval_context(db):
    user_model = get_user_model()
    admin = user_model.objects.create_user(username="approval-admin", password="test-password")
    editor = user_model.objects.create_user(username="approval-editor", password="test-password")
    tenant = Tenant.objects.create(name="Approval tenant", slug="approval-tenant")
    TenantMembership.objects.create(tenant=tenant, user=admin, role=TenantMembership.Role.ADMIN)
    TenantMembership.objects.create(tenant=tenant, user=editor, role=TenantMembership.Role.MEMBER)
    project = Project.objects.create(
        tenant=tenant,
        name="Approval project",
        slug="approval-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=admin,
        role=ProjectMembership.Role.ADMIN,
    )
    ProjectMembership.objects.create(
        project=project,
        user=editor,
        role=ProjectMembership.Role.EDITOR,
    )
    plugin = PluginDefinition.objects.create(
        tenant=tenant,
        created_by=admin,
        plugin_key="approval-demo",
        name="Approval demo",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.NATIVE,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
    )
    tool = PluginTool.objects.create(
        plugin=plugin,
        name="update",
        description="Update a protected resource",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        enabled=True,
        risk_level="high",
    )
    installation = PluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=PluginInstallation.Status.INSTALLED,
        granted_permissions=[],
        installed_by=admin,
    )

    def create_pending(key):
        task = Task.objects.create(
            tenant=tenant,
            project=project,
            created_by=admin,
            task_type="agent.run",
            payload={},
            idempotency_key=key,
            status=Task.Status.WAITING_APPROVAL,
            available_at=timezone.now(),
        )
        approval = ToolApprovalRequest.objects.create(
            task=task,
            tenant=tenant,
            project=project,
            tool=tool,
            installation=installation,
            call_id=f"call-{key}",
            arguments={"resource": "cluster-a", "api_token": "do-not-return"},
            arguments_hash="a" * 64,
            plugin_version=plugin.version,
            execution_checkpoint={
                "version": 1,
                "messages": [{"role": "user", "content": "update"}],
                "raw_tool_calls": [],
                "call_index": 0,
                "iteration": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0,
                "total_tool_calls": 1,
                "max_iterations": 4,
                "model_config_id": "model-id",
            },
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return task, approval

    return {
        "admin": admin,
        "editor": editor,
        "tenant": tenant,
        "project": project,
        "tool": tool,
        "installation": installation,
        "create_pending": create_pending,
    }


@pytest.mark.django_db
def test_project_admin_can_list_redacted_tool_approvals(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("list")
    client = APIClient()
    client.force_authenticate(user=ctx["admin"])
    url = (
        f"/api/v1/tenants/{ctx['tenant'].id}/projects/"
        f"{ctx['project'].id}/tool-approvals/"
    )

    response = client.get(url)

    assert response.status_code == 200
    assert response.data["count"] == 1
    item = response.data["items"][0]
    assert item["id"] == str(approval.id)
    assert item["arguments"]["resource"] == "cluster-a"
    assert item["arguments"]["api_token"] == "[REDACTED]"
    assert "execution_checkpoint" not in item
    assert item["task_id"] == str(task.id)


@pytest.mark.django_db
def test_project_admin_approval_requeues_task_for_worker_resume(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("approve")
    client = APIClient()
    client.force_authenticate(user=ctx["admin"])
    url = (
        f"/api/v1/tenants/{ctx['tenant'].id}/projects/"
        f"{ctx['project'].id}/tool-approvals/{approval.id}/approve/"
    )

    response = client.post(url, {}, format="json")

    assert response.status_code == 202
    approval.refresh_from_db()
    task.refresh_from_db()
    assert approval.status == ToolApprovalRequest.Status.APPROVED
    assert approval.approved_by_id == ctx["admin"].id
    assert approval.approved_at is not None
    assert task.status == Task.Status.QUEUED
    assert task.available_at <= timezone.now()
    assert task.error is None


@pytest.mark.django_db
def test_project_admin_can_reject_and_stop_waiting_task(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("reject")
    client = APIClient()
    client.force_authenticate(user=ctx["admin"])
    url = (
        f"/api/v1/tenants/{ctx['tenant'].id}/projects/"
        f"{ctx['project'].id}/tool-approvals/{approval.id}/reject/"
    )

    response = client.post(url, {"reason": "Change not authorized"}, format="json")

    assert response.status_code == 202
    approval.refresh_from_db()
    task.refresh_from_db()
    assert approval.status == ToolApprovalRequest.Status.REJECTED
    assert approval.rejection_reason == "Change not authorized"
    assert approval.rejected_by_id == ctx["admin"].id
    assert approval.rejected_at is not None
    assert approval.approved_by_id is None
    assert task.status == Task.Status.FAILED
    assert task.error["code"] == "TOOL_APPROVAL_REJECTED"


@pytest.mark.django_db
def test_project_editor_cannot_approve_high_risk_tool_call(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("denied")
    client = APIClient()
    client.force_authenticate(user=ctx["editor"])
    url = (
        f"/api/v1/tenants/{ctx['tenant'].id}/projects/"
        f"{ctx['project'].id}/tool-approvals/{approval.id}/approve/"
    )

    response = client.post(url, {}, format="json")

    assert response.status_code == 403
    approval.refresh_from_db()
    task.refresh_from_db()
    assert approval.status == ToolApprovalRequest.Status.PENDING
    assert task.status == Task.Status.WAITING_APPROVAL


@pytest.mark.django_db
def test_expired_approval_fails_closed(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("expired")
    approval.expires_at = timezone.now() - timedelta(seconds=1)
    approval.save(update_fields=["expires_at"])
    client = APIClient()
    client.force_authenticate(user=ctx["admin"])
    url = (
        f"/api/v1/tenants/{ctx['tenant'].id}/projects/"
        f"{ctx['project'].id}/tool-approvals/{approval.id}/approve/"
    )

    response = client.post(url, {}, format="json")

    assert response.status_code == 409
    approval.refresh_from_db()
    task.refresh_from_db()
    assert approval.status == ToolApprovalRequest.Status.EXPIRED
    assert task.status == Task.Status.FAILED


@pytest.mark.django_db
def test_cancelling_waiting_task_cancels_its_pending_approval(approval_context):
    ctx = approval_context
    task, approval = ctx["create_pending"]("cancel")
    client = APIClient()
    client.force_authenticate(user=ctx["admin"])

    response = client.post(f"/api/v1/tasks/{task.id}/cancel/", {}, format="json")

    assert response.status_code == 202
    task.refresh_from_db()
    approval.refresh_from_db()
    assert task.status == Task.Status.CANCELLED
    assert approval.status == ToolApprovalRequest.Status.CANCELLED
