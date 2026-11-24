import json
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.models import (
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowVersion,
)
from core.workflow_engine import (
    WorkflowValidationError,
    expire_workflow_runs,
    validate_model_references,
    validate_workflow_definition,
)
from core.workflow_serializers import (
    WorkflowDraftSerializer,
    WorkflowRunSerializer,
    WorkflowVersionSerializer,
)

DEFAULT_WORKFLOW_DEADLINE_SECONDS = 86400
MIN_WORKFLOW_DEADLINE_SECONDS = 60
MAX_WORKFLOW_DEADLINE_SECONDS = 604800


def workflow_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def workflow_project(request, tenant, project_id):
    queryset = Project.objects.filter(tenant=tenant)
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


def can_edit_workflow_project(request, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_editor = ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()
    return tenant_admin or project_editor


def get_workflow(request, workflow_id):
    queryset = WorkflowDefinition.objects.filter(tenant__memberships__user=request.user)
    tenant_admin = TenantMembership.objects.filter(
        tenant__workflows__id=workflow_id,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(project__memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=workflow_id)


def can_edit_workflow(request, workflow):
    return can_edit_workflow_project(request, workflow.tenant, workflow.project)


class WorkflowListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = workflow_tenant(request, tenant_id)
        project = workflow_project(request, tenant, project_id)
        workflows = WorkflowDefinition.objects.filter(
            tenant=tenant,
            project=project,
            is_archived=False,
        ).order_by("name")
        return Response(WorkflowDraftSerializer(workflows, many=True).data)

    def post(self, request, tenant_id, project_id):
        tenant = workflow_tenant(request, tenant_id)
        project = workflow_project(request, tenant, project_id)
        if not can_edit_workflow_project(request, tenant, project):
            return legacy_error_response(
                "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        serializer = WorkflowDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow = serializer.save(
            tenant=tenant,
            project=project,
            created_by=request.user,
        )
        record_audit_event(
            request,
            tenant,
            "workflow.created",
            "workflow_definition",
            workflow.id,
            metadata={
                "slug": workflow.slug,
                "name": workflow.name,
                "project_id": str(project.id),
            },
        )
        return Response(
            WorkflowDraftSerializer(workflow).data,
            status=status.HTTP_201_CREATED,
        )


class WorkflowDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        return Response(WorkflowDraftSerializer(workflow).data)

    def patch(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        if not can_edit_workflow(request, workflow):
            return legacy_error_response(
                "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        if workflow.is_archived:
            return legacy_error_response(
                "Archived workflows are read-only.", http_status=status.HTTP_409_CONFLICT
            )
        serializer = WorkflowDraftSerializer(workflow, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        previous_version = workflow.current_version
        serializer.save()
        record_audit_event(
            request,
            workflow.tenant,
            "workflow.updated",
            "workflow_definition",
            workflow.id,
            metadata={
                "fields": sorted(serializer.validated_data.keys()),
                "previous_current_version": previous_version,
            },
        )
        return Response(serializer.data)


class WorkflowValidateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        if not can_edit_workflow(request, workflow):
            return legacy_error_response(
                "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        definition = workflow.draft_definition
        node_values = definition.get("nodes") if isinstance(definition, dict) else None
        edge_values = definition.get("edges") if isinstance(definition, dict) else None
        node_count = len(node_values) if isinstance(node_values, list) else 0
        edge_count = len(edge_values) if isinstance(edge_values, list) else 0
        try:
            graph = validate_workflow_definition(definition)
            validate_model_references(graph, workflow.tenant, workflow.project)
        except WorkflowValidationError as exc:
            return Response(
                {
                    "is_valid": False,
                    "errors": [str(exc)],
                    "execution_layers": [],
                    "node_count": node_count,
                    "edge_count": edge_count,
                },
                status=status.HTTP_200_OK,
            )

        depths = {}
        layers = {}
        for node_id in graph["order"]:
            incoming = graph["incoming"][node_id]
            depth = max(
                (depths[edge["source"]] + 1 for edge in incoming),
                default=0,
            )
            depths[node_id] = depth
            layers.setdefault(depth, []).append(node_id)
        return Response(
            {
                "is_valid": True,
                "errors": [],
                "execution_layers": [layers[index] for index in sorted(layers)],
                "node_count": node_count,
                "edge_count": edge_count,
            }
        )


class WorkflowPublishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workflow_id):
        with transaction.atomic():
            workflow = get_workflow(request, workflow_id)
            if not can_edit_workflow(request, workflow):
                return legacy_error_response(
                    "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
                )
            workflow = WorkflowDefinition.objects.select_for_update().get(pk=workflow.pk)
            if workflow.is_archived:
                return legacy_error_response(
                    "Archived workflows cannot be published.", http_status=status.HTTP_409_CONFLICT
                )
            try:
                graph = validate_workflow_definition(workflow.draft_definition)
                validate_model_references(graph, workflow.tenant, workflow.project)
            except WorkflowValidationError as exc:
                return legacy_error_response(str(exc), http_status=status.HTTP_400_BAD_REQUEST)
            version_number = (
                WorkflowVersion.objects.filter(workflow=workflow)
                .order_by("-version")
                .values_list("version", flat=True)
                .first()
                or 0
            ) + 1
            version = WorkflowVersion.objects.create(
                workflow=workflow,
                version=version_number,
                definition_snapshot=workflow.draft_definition,
                created_by=request.user,
            )
            workflow.current_version = version_number
            workflow.save(update_fields=["current_version", "updated_at"])
            record_audit_event(
                request,
                workflow.tenant,
                "workflow.published",
                "workflow_version",
                version.id,
                metadata={
                    "workflow_id": str(workflow.id),
                    "version": version_number,
                    "step_count": len(graph.get("nodes", {})),
                },
            )
        return Response(
            WorkflowVersionSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )


class WorkflowRunListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        runs = (
            WorkflowRun.objects.filter(workflow_version__workflow=workflow)
            .select_related("task", "workflow_version")
            .prefetch_related("steps")
            .order_by("-created_at")
        )
        return Response(WorkflowRunSerializer(runs, many=True).data)

    def post(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        if workflow.is_archived or workflow.current_version <= 0:
            return legacy_error_response(
                "Workflow must have a published version before it can run.",
                http_status=status.HTTP_409_CONFLICT,
            )
        if not isinstance(request.data, dict):
            return legacy_error_response(
                "Request body must be a JSON object.", http_status=status.HTTP_400_BAD_REQUEST
            )
        input_data = request.data.get("input", {})
        deadline_seconds = request.data.get("deadline_seconds", DEFAULT_WORKFLOW_DEADLINE_SECONDS)
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, int)
            or not MIN_WORKFLOW_DEADLINE_SECONDS
            <= deadline_seconds
            <= MAX_WORKFLOW_DEADLINE_SECONDS
        ):
            return legacy_error_response(
                "deadline_seconds must be an integer between 60 and 604800.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not isinstance(input_data, dict) or len(json.dumps(input_data)) > 65536:
            return legacy_error_response(
                "input must be a JSON object no larger than 64 KiB.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        if not idempotency_key or len(idempotency_key) > 128:
            return legacy_error_response(
                "A valid Idempotency-Key header is required.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        version = get_object_or_404(
            WorkflowVersion,
            workflow=workflow,
            version=workflow.current_version,
        )
        payload = {
            "workflow_version_id": str(version.id),
            "input_data": input_data,
            "deadline_seconds": deadline_seconds,
        }
        with transaction.atomic():
            task, created = Task.objects.get_or_create(
                tenant=workflow.tenant,
                idempotency_key=idempotency_key,
                defaults={
                    "project": workflow.project,
                    "created_by": request.user,
                    "task_type": "workflow.run",
                    "payload": payload,
                    "available_at": timezone.now(),
                },
            )
            if not created and (
                task.project_id != workflow.project_id
                or task.task_type != "workflow.run"
                or task.payload != payload
            ):
                return legacy_error_response(
                    "Idempotency-Key was already used with a different request.",
                    http_status=status.HTTP_409_CONFLICT,
                )
            run, _ = WorkflowRun.objects.get_or_create(
                task=task,
                defaults={
                    "workflow_version": version,
                    "input_data": input_data,
                    "state": {"input": input_data},
                    "deadline_at": timezone.now() + timedelta(seconds=deadline_seconds),
                },
            )
        record_audit_event(
            request,
            workflow.tenant,
            "workflow.run.created",
            "workflow_run",
            run.id,
            metadata={
                "workflow_id": str(workflow.id),
                "task_id": str(task.id),
                "workflow_version_id": str(version.id),
                "deduplicated": not created,
            },
        )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(
            WorkflowRunSerializer(run).data,
            status=response_status,
        )


class WorkflowRunDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        run = get_object_or_404(
            WorkflowRun.objects.select_related(
                "task",
                "workflow_version",
                "workflow_version__workflow",
            )
            .prefetch_related("steps")
            .filter(
                workflow_version__workflow__tenant__memberships__user=request.user,
            ),
            pk=run_id,
        )
        workflow = run.workflow_version.workflow
        tenant_admin = TenantMembership.objects.filter(
            tenant=workflow.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if (
            not tenant_admin
            and not ProjectMembership.objects.filter(
                project=workflow.project,
                user=request.user,
            ).exists()
        ):
            return legacy_error_response("Not found.", http_status=status.HTTP_404_NOT_FOUND)
        return Response(WorkflowRunSerializer(run).data)


def _workflow_run_for_user(request, run_id):
    queryset = WorkflowRun.objects.select_related(
        "task",
        "workflow_version",
        "workflow_version__workflow",
    ).filter(
        Q(workflow_version__workflow__tenant__memberships__user=request.user)
        | Q(workflow_version__workflow__project__memberships__user=request.user)
    )
    return get_object_or_404(queryset.distinct(), pk=run_id)


def _can_decide_workflow_approval(request, workflow):
    tenant_admin = TenantMembership.objects.filter(
        tenant=workflow.tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_admin = ProjectMembership.objects.filter(
        project=workflow.project,
        user=request.user,
        role=ProjectMembership.Role.ADMIN,
    ).exists()
    return tenant_admin or project_admin


class WorkflowStepApprovalView(APIView):
    permission_classes = [IsAuthenticated]

    decision = None

    def post(self, request, run_id, node_id):
        reason = request.data.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 500:
            return legacy_error_response(
                "reason must be a string no longer than 500 characters.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        run = _workflow_run_for_user(request, run_id)
        workflow = run.workflow_version.workflow
        if not _can_decide_workflow_approval(request, workflow):
            return legacy_error_response(
                "Project administrator permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        if run.deadline_at and run.deadline_at <= timezone.now():
            expire_workflow_runs(limit=100)
            return legacy_error_response(
                "Workflow run deadline exceeded before the decision.",
                http_status=status.HTTP_409_CONFLICT,
            )
        now = timezone.now()
        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=run.task_id)
            step = get_object_or_404(
                WorkflowStepRun.objects.select_for_update(),
                workflow_run=run,
                node_id=node_id,
                node_type="approval",
            )
            if step.status != WorkflowStepRun.Status.AWAITING_APPROVAL:
                return legacy_error_response(
                    "Workflow step is not awaiting approval.", http_status=status.HTTP_409_CONFLICT
                )
            if task.status != Task.Status.WAITING_APPROVAL:
                return legacy_error_response(
                    "Workflow task is no longer waiting for approval.",
                    http_status=status.HTTP_409_CONFLICT,
                )
            decision_payload = {
                "decision": self.decision,
                "decided_by_id": request.user.id,
                "decided_at": now.isoformat(),
                "reason": reason.strip(),
            }
            if self.decision == "approved":
                step.status = WorkflowStepRun.Status.SUCCEEDED
                step.output_data = decision_payload
                step.finished_at = now
                step.save(update_fields=["status", "output_data", "finished_at"])
                task.status = Task.Status.QUEUED
                task.available_at = now
                task.attempts = 0
                task.error = None
                task.lease_owner = None
                task.lease_token = None
                task.lease_expires_at = None
                task.save(
                    update_fields=[
                        "status",
                        "available_at",
                        "attempts",
                        "error",
                        "lease_owner",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ]
                )
            else:
                step.status = WorkflowStepRun.Status.FAILED
                step.error = {
                    "code": "WORKFLOW_APPROVAL_REJECTED",
                    "message": "A project administrator rejected this workflow step.",
                }
                step.output_data = decision_payload
                step.finished_at = now
                step.save(update_fields=["status", "error", "output_data", "finished_at"])
                task.status = Task.Status.FAILED
                task.error = {
                    "code": "WORKFLOW_APPROVAL_REJECTED",
                    "message": "A project administrator rejected this workflow step.",
                    "retryable": False,
                }
                task.finished_at = now
                task.lease_owner = None
                task.lease_token = None
                task.lease_expires_at = None
                task.save(
                    update_fields=[
                        "status",
                        "error",
                        "finished_at",
                        "lease_owner",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ]
                )
                run.finished_at = now
                run.save(update_fields=["finished_at"])
        record_audit_event(
            request,
            workflow.tenant,
            f"workflow.approval.{self.decision}",
            "workflow_step_run",
            step.id,
            project=workflow.project,
            metadata={
                "workflow_run_id": str(run.id),
                "workflow_id": str(workflow.id),
                "node_id": node_id,
                "reason": reason.strip(),
            },
        )
        return Response(
            {
                "run_id": str(run.id),
                "node_id": node_id,
                "decision": self.decision,
                "task_status": Task.Status.QUEUED
                if self.decision == "approved"
                else Task.Status.FAILED,
            }
        )


class WorkflowStepApproveView(WorkflowStepApprovalView):
    decision = "approved"


class WorkflowStepRejectView(WorkflowStepApprovalView):
    decision = "rejected"
