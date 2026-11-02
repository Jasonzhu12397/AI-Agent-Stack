from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET
from rest_framework.generics import ListAPIView, ListCreateAPIView
from rest_framework.permissions import IsAuthenticated

from core.models import Project, ProjectMembership, Tenant
from core.pagination import TenantPagination
from core.permissions import IsTenantAdmin
from core.serializers import ProjectSerializer, TenantSerializer


@require_GET
def liveness(request):
    return JsonResponse({"status": "ok"})


@require_GET
def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "not_ready", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ready", "database": "ok"})


class TenantListView(ListAPIView):
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = TenantPagination

    def get_queryset(self):
        return (
            Tenant.objects.filter(memberships__user=self.request.user)
            .order_by("name")
            .distinct()
        )


class TenantProjectListView(ListCreateAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = TenantPagination

    def get_permissions(self):
        permissions = [IsAuthenticated]
        if self.request.method == "POST":
            permissions.append(IsTenantAdmin)
        return [permission() for permission in permissions]

    def get_tenant(self):
        if not hasattr(self, "_tenant"):
            self._tenant = get_object_or_404(
                Tenant.objects.filter(memberships__user=self.request.user),
                pk=self.kwargs["tenant_id"],
            )
        return self._tenant

    def get_queryset(self):
        return (
            Project.objects.filter(
                tenant=self.get_tenant(),
                memberships__user=self.request.user,
            )
            .order_by("name")
            .distinct()
        )

    def perform_create(self, serializer):
        project = serializer.save(tenant=self.get_tenant())
        ProjectMembership.objects.create(
            project=project,
            user=self.request.user,
            role=ProjectMembership.Role.ADMIN,
        )
