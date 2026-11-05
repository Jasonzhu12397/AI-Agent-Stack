import json

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import (
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from core.workflow_engine import (
    WorkflowValidationError,
    validate_model_references,
    validate_workflow_definition,
)
from core.workflow_serializers import (
    WorkflowDraftSerializer,
    WorkflowRunSerializer,
    WorkflowVersionSerializer,
)


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
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = WorkflowDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow = serializer.save(
            tenant=tenant,
            project=project,
            created_by=request.user,
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
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if workflow.is_archived:
            return Response(
                {"detail": "Archived workflows are read-only."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = WorkflowDraftSerializer(workflow, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)



class WorkflowValidateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workflow_id):
        workflow = get_workflow(request, workflow_id)
        if not can_edit_workflow(request, workflow):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        definition = workflow.draft_definition
        node_count = len(definition.get("nodes", [])) if isinstance(definition, dict) and isinstance(definition.get("nodes"), list) else 0
        edge_count = len(definition.get("edges", [])) if isinstance(definition, dict) and isinstance(definition.get("edges"), list) else 0
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
                status=status.HTTP_400_BAD_REQUEST,
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
                return Response(
                    {"detail": "Project editor permission required."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            workflow = WorkflowDefinition.objects.select_for_update().get(pk=workflow.pk)
            if workflow.is_archived:
                return Response(
                    {"detail": "Archived workflows cannot be published."},
                    status=status.HTTP_409_CONFLICT,
                )
            try:
                graph = validate_workflow_definition(workflow.draft_definition)
                validate_model_references(graph, workflow.tenant, workflow.project)
            except WorkflowValidationError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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
            return Response(
                {"detail": "Workflow must have a published version before it can run."},
                status=status.HTTP_409_CONFLICT,
            )
        if not isinstance(request.data, dict):
            return Response(
                {"detail": "Request body must be a JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        input_data = request.data.get("input", {})
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not isinstance(input_data, dict) or len(json.dumps(input_data)) > 65536:
            return Response(
                {"detail": "input must be a JSON object no larger than 64 KiB."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not idempotency_key or len(idempotency_key) > 128:
            return Response(
                {"detail": "A valid Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        version = get_object_or_404(
            WorkflowVersion,
            workflow=workflow,
            version=workflow.current_version,
        )
        payload = {
            "workflow_version_id": str(version.id),
            "input_data": input_data,
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
                return Response(
                    {"detail": "Idempotency-Key was already used with a different request."},
                    status=status.HTTP_409_CONFLICT,
                )
            run, _ = WorkflowRun.objects.get_or_create(
                task=task,
                defaults={
                    "workflow_version": version,
                    "input_data": input_data,
                    "state": {"input": input_data},
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
        if not tenant_admin and not ProjectMembership.objects.filter(
            project=workflow.project,
            user=request.user,
        ).exists():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(WorkflowRunSerializer(run).data)
