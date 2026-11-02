import json

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Project, ProjectMembership, Task, Tenant, TenantMembership
from core.serializers import TaskSerializer


SUPPORTED_TASK_TYPES = {"demo.echo", "demo.sleep"}


def accessible_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def accessible_project(request, tenant, project_id):
    queryset = Project.objects.filter(tenant=tenant)
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


def can_create_task(request, tenant, project):
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


def visible_task_queryset(request):
    return Task.objects.filter(
        tenant__memberships__user=request.user,
    ).filter(
        project__memberships__user=request.user,
    ).distinct()


class TaskListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        queryset = Task.objects.filter(tenant=tenant, project=project).order_by("-created_at")
        limit = min(max(int(request.query_params.get("limit", 50)), 1), 100)
        offset = max(int(request.query_params.get("offset", 0)), 0)
        items = queryset[offset : offset + limit]
        return Response(
            {
                "count": queryset.count(),
                "limit": limit,
                "offset": offset,
                "results": TaskSerializer(items, many=True).data,
            }
        )

    def post(self, request, tenant_id, project_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        if not can_create_task(request, tenant, project):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        task_type = request.data.get("task_type")
        payload = request.data.get("payload", {})
        if not idempotency_key or len(idempotency_key) > 128:
            return Response(
                {"detail": "A valid Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if task_type not in SUPPORTED_TASK_TYPES:
            return Response(
                {"detail": "Unsupported task_type."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 65536:
            return Response(
                {"detail": "payload must be a JSON object no larger than 64 KiB."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            with transaction.atomic():
                task, created = Task.objects.get_or_create(
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                    defaults={
                        "project": project,
                        "created_by": request.user,
                        "task_type": task_type,
                        "payload": payload,
                        "available_at": timezone.now(),
                    },
                )
        except IntegrityError:
            task = Task.objects.get(tenant=tenant, idempotency_key=idempotency_key)
            created = False
        if not created and (
            task.project_id != project.id
            or task.task_type != task_type
            or task.payload != payload
        ):
            return Response(
                {"detail": "Idempotency-Key was already used with a different request."},
                status=status.HTTP_409_CONFLICT,
            )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(TaskSerializer(task).data, status=response_status)


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_task(self, request, task_id):
        queryset = Task.objects.filter(tenant__memberships__user=request.user)
        tenant_admin = TenantMembership.objects.filter(
            tenant__tasks__id=task_id,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not tenant_admin:
            queryset = queryset.filter(project__memberships__user=request.user)
        return get_object_or_404(queryset.distinct(), pk=task_id)

    def get(self, request, task_id):
        return Response(TaskSerializer(self.get_task(request, task_id)).data)


class TaskCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        task = TaskDetailView().get_task(request, task_id)
        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=task.pk)
            if task.status == Task.Status.QUEUED:
                task.status = Task.Status.CANCELLED
                task.finished_at = timezone.now()
            elif task.status == Task.Status.RUNNING:
                task.status = Task.Status.CANCEL_REQUESTED
                task.cancel_requested_at = timezone.now()
            elif task.status == Task.Status.CANCEL_REQUESTED:
                return Response(TaskSerializer(task).data, status=status.HTTP_202_ACCEPTED)
            else:
                return Response(
                    {"detail": "Task is already in a terminal or approval state."},
                    status=status.HTTP_409_CONFLICT,
                )
            task.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "cancel_requested_at",
                    "updated_at",
                ]
            )
        return Response(
            TaskSerializer(task).data,
            status=status.HTTP_202_ACCEPTED,
        )
