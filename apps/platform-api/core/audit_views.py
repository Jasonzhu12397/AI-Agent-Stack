from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import verify_audit_chain
from core.models import AuditEvent, Project, Tenant, TenantMembership


def _audit_event_data(event):
    return {
        "id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "project_id": str(event.project_id) if event.project_id else None,
        "actor_id": event.actor_id,
        "actor_username": event.actor.get_username() if event.actor else None,
        "event_type": event.event_type,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "outcome": event.outcome,
        "request_id": event.request_id,
        "remote_addr": event.remote_addr,
        "user_agent": event.user_agent,
        "metadata": event.metadata,
        "previous_hash": event.previous_hash,
        "event_hash": event.event_hash,
        "created_at": event.created_at,
    }


class AuditEventListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = Tenant.objects.filter(
            memberships__user=request.user,
        ).distinct().filter(pk=tenant_id).first()
        if tenant is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        is_admin = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response(
                {"detail": "Tenant administrator permission required to view audit events."},
                status=status.HTTP_403_FORBIDDEN,
            )
        project_id = request.query_params.get("project_id")
        events = AuditEvent.objects.filter(tenant=tenant).select_related("actor", "project")
        if project_id:
            project = Project.objects.filter(tenant=tenant, pk=project_id).first()
            if project is None:
                return Response({"detail": "Project not found."}, status=404)
            events = events.filter(project=project)
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return Response({"detail": "limit and offset must be integers."}, status=400)
        if not 1 <= limit <= 100 or offset < 0:
            return Response({"detail": "limit must be 1–100 and offset must be non-negative."}, status=400)
        total = events.count()
        page = events.order_by("-created_at", "-id")[offset : offset + limit]
        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": [_audit_event_data(event) for event in page],
            }
        )


class AuditEventVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = Tenant.objects.filter(
            memberships__user=request.user,
        ).distinct().filter(pk=tenant_id).first()
        if tenant is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        is_admin = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response(
                {"detail": "Tenant administrator permission required to verify audit events."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(verify_audit_chain(tenant))
