from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from core.models import Project, Tenant
from core.pagination import TenantPagination
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
        return Tenant.objects.filter(memberships__user=self.request.user).order_by("name").distinct()


class TenantProjectListView(ListAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = TenantPagination

    def get_queryset(self):
        tenant = get_object_or_404(
            Tenant.objects.filter(memberships__user=self.request.user),
            pk=self.kwargs["tenant_id"],
        )
        return Project.objects.filter(
            tenant=tenant,
            memberships__user=self.request.user,
        ).order_by("name").distinct()
