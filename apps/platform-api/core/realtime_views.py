from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import TenantMembership
from core.realtime import event_stream
from core.task_views import accessible_project, accessible_tenant


class ProjectEventStreamView(APIView):
    """Server-sent events for task and audit changes in one project."""

    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        since = request.headers.get("Last-Event-ID") or request.query_params.get("since")
        max_events = request.query_params.get("max_events")
        if max_events is not None:
            try:
                max_events = int(max_events)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "max_events must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not 1 <= max_events <= 100:
                return Response(
                    {"detail": "max_events must be between 1 and 100."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        audit_visible = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        response = StreamingHttpResponse(
            event_stream(
                tenant.id,
                project.id,
                since,
                audit_visible,
                max_events=max_events,
            ),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        response["Connection"] = "keep-alive"
        return response
