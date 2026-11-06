# ruff: noqa: I001
import re

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.models import (
    AgentRun,
    ProjectMembership,
    Task,
    TenantMembership,
    ToolApprovalRequest,
)
from core.task_views import accessible_project, accessible_tenant


SENSITIVE_ARGUMENT_KEYS = re.compile(
    r"(password|secret|token|api[_-]?key|credential|private[_-]?key|authorization)",
    re.IGNORECASE,
)


def _redact_arguments(value):
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if SENSITIVE_ARGUMENT_KEYS.search(str(key))
                else _redact_arguments(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[UNSUPPORTED]"


def _can_approve(request, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_admin = ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role=ProjectMembership.Role.ADMIN,
    ).exists()
    return tenant_admin or project_admin


def _approval_data(approval):
    return {
        "id": str(approval.id),
        "task_id": str(approval.task_id),
        "tenant_id": str(approval.tenant_id),
        "project_id": str(approval.project_id),
        "tool": approval.tool.name,
        "plugin": approval.tool.plugin.name,
        "plugin_version": approval.plugin_version,
        "installation_id": str(approval.installation_id),
        "connection_id": str(approval.connection_id) if approval.connection_id else None,
        "call_id": approval.call_id,
        "arguments": _redact_arguments(approval.arguments),
        "arguments_hash": approval.arguments_hash,
        "status": approval.status,
        "requested_at": approval.requested_at,
        "expires_at": approval.expires_at,
        "approved_at": approval.approved_at,
        "consumed_at": approval.consumed_at,
        "approved_by_id": approval.approved_by_id,
        "rejected_by_id": approval.rejected_by_id,
        "rejected_at": approval.rejected_at,
        "rejection_reason": approval.rejection_reason,
    }


class ToolApprovalListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        if not _can_approve(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        queryset = ToolApprovalRequest.objects.filter(
            tenant=tenant,
            project=project,
        ).select_related("tool__plugin", "approved_by")
        requested_status = request.query_params.get("status")
        if requested_status:
            valid_statuses = {choice for choice, _ in ToolApprovalRequest.Status.choices}
            if requested_status not in valid_statuses:
                return Response(
                    {"detail": "Unsupported approval status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(status=requested_status)
        items = [_approval_data(item) for item in queryset[:100]]
        return Response({"items": items, "count": len(items)})


class ToolApprovalDecisionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, approval_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        if not _can_approve(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        decision = self.decision
        reason = request.data.get("reason", "")
        if decision == "reject" and (
            not isinstance(reason, str) or len(reason) > 500
        ):
            return Response(
                {"detail": "reason must be a string no longer than 500 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            approval = get_object_or_404(
                ToolApprovalRequest.objects.select_for_update(),
                pk=approval_id,
                tenant=tenant,
                project=project,
            )
            task = Task.objects.select_for_update().get(pk=approval.task_id)
            now = timezone.now()
            if approval.status != ToolApprovalRequest.Status.PENDING:
                return Response(
                    {"detail": "Approval request is no longer pending."},
                    status=status.HTTP_409_CONFLICT,
                )
            if approval.expires_at <= now:
                approval.status = ToolApprovalRequest.Status.EXPIRED
                approval.save(update_fields=["status"])
                if task.status == Task.Status.WAITING_APPROVAL:
                    task.status = Task.Status.FAILED
                    task.finished_at = now
                    task.error = {
                        "code": "TOOL_APPROVAL_EXPIRED",
                        "message": "The high-risk tool approval request expired.",
                    }
                    task.save(
                        update_fields=["status", "finished_at", "error", "updated_at"]
                    )
                    AgentRun.objects.filter(task=task, finished_at__isnull=True).update(
                        finished_at=now
                    )
                expired = True
            elif task.status != Task.Status.WAITING_APPROVAL:
                return Response(
                    {"detail": "Task is no longer waiting for approval."},
                    status=status.HTTP_409_CONFLICT,
                )
            elif decision == "approve":
                approval.status = ToolApprovalRequest.Status.APPROVED
                approval.approved_by = request.user
                approval.approved_at = now
                approval.save(
                    update_fields=["status", "approved_by", "approved_at"]
                )
                task.status = Task.Status.QUEUED
                task.available_at = now
                task.error = None
                task.lease_owner = None
                task.lease_token = None
                task.lease_expires_at = None
                task.save(
                    update_fields=[
                        "status",
                        "available_at",
                        "error",
                        "lease_owner",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ]
                )
                expired = False
            else:
                approval.status = ToolApprovalRequest.Status.REJECTED
                approval.rejected_by = request.user
                approval.rejected_at = now
                approval.rejection_reason = reason.strip()
                approval.save(
                    update_fields=[
                        "status",
                        "rejected_by",
                        "rejected_at",
                        "rejection_reason",
                    ]
                )
                task.status = Task.Status.FAILED
                task.finished_at = now
                task.error = {
                    "code": "TOOL_APPROVAL_REJECTED",
                    "message": "A project administrator rejected a high-risk tool call.",
                }
                task.save(
                    update_fields=["status", "finished_at", "error", "updated_at"]
                )
                AgentRun.objects.filter(task=task, finished_at__isnull=True).update(
                    finished_at=now
                )
                expired = False

        if expired:
            record_audit_event(
                request,
                tenant,
                "plugin.tool_approval.expired",
                "tool_approval",
                approval.id,
                project=project,
                outcome="failure",
                metadata={"task_id": str(task.id), "tool_id": str(approval.tool_id)},
            )
            return Response(
                {"detail": "Approval request expired; task was stopped."},
                status=status.HTTP_409_CONFLICT,
            )

        event_type = (
            "plugin.tool_approval.approved"
            if decision == "approve"
            else "plugin.tool_approval.rejected"
        )
        record_audit_event(
            request,
            tenant,
            event_type,
            "tool_approval",
            approval.id,
            project=project,
            metadata={
                "task_id": str(task.id),
                "tool_id": str(approval.tool_id),
                "arguments_hash": approval.arguments_hash,
            },
        )
        task.refresh_from_db()
        approval.refresh_from_db()
        return Response(
            {
                "approval": _approval_data(approval),
                "task_status": task.status,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ToolApprovalApproveView(ToolApprovalDecisionView):
    decision = "approve"


class ToolApprovalRejectView(ToolApprovalDecisionView):
    decision = "reject"
