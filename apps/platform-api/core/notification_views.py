"""HTTP surface for IM group-robot notification endpoints.

The tenant sees which robots exist and whether one is delivering; the webhook
URL and signing secret are write-only. Sending a test message is the operator's
way to prove a bot works before it carries real alerts.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.field_crypto import seal
from core.models import AuditEvent, Tenant, TenantMembership
from core.notification_channels import NotificationChannelError, deliver
from core.settings_models import NotificationEndpoint

_SCOPE = "notification-endpoint"


def _error(code: str, message: str, http_status: int = 400):
    return Response({"error": {"code": code, "message": message}}, status=http_status)


def _is_admin(request, tenant) -> bool:
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()


def _tenant(request, tenant_id) -> Tenant:
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def _endpoint_data(endpoint: NotificationEndpoint) -> dict:
    return {
        "id": str(endpoint.id),
        "channel": endpoint.channel,
        "name": endpoint.name,
        "enabled": endpoint.enabled,
        "last_sent_at": endpoint.last_sent_at,
        "last_error": endpoint.last_error,
    }


class NotificationEndpointListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        _tenant(request, tenant_id)
        endpoints = NotificationEndpoint.objects.filter(tenant_id=tenant_id)
        return Response({"results": [_endpoint_data(item) for item in endpoints]})

    def post(self, request, tenant_id):
        tenant = _tenant(request, tenant_id)
        if not _is_admin(request, tenant):
            return _error(
                "TENANT_ADMIN_REQUIRED",
                "Tenant owner or admin role is required to add a notification endpoint.",
                status.HTTP_403_FORBIDDEN,
            )
        channel = str(request.data.get("channel", "")).strip()
        if channel not in dict(NotificationEndpoint.Channel.choices):
            return _error("INVALID_CHANNEL", "channel must be dingtalk, wecom or feishu.")
        name = str(request.data.get("name", "")).strip()
        url = str(request.data.get("webhook_url", "")).strip()
        if not name or not url:
            return _error("INVALID_ENDPOINT", "name and webhook_url are required.")
        if not url.startswith("https://"):
            return _error("INVALID_ENDPOINT", "webhook_url must be an https URL.")
        endpoint = NotificationEndpoint.objects.create(
            tenant=tenant,
            channel=channel,
            name=name[:120],
            sealed_url=seal(url, _SCOPE),
            sealed_secret=seal(str(request.data.get("secret", "")).strip(), _SCOPE),
            enabled=bool(request.data.get("enabled", True)),
        )
        record_audit_event(
            request,
            tenant,
            "settings.notification.endpoint",
            "notification_endpoint",
            endpoint.id,
            metadata={"channel": channel, "name": name},
        )
        return Response(_endpoint_data(endpoint), status=status.HTTP_201_CREATED)


class NotificationEndpointDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, tenant_id, endpoint_id):
        tenant = _tenant(request, tenant_id)
        endpoint = get_object_or_404(NotificationEndpoint, tenant_id=tenant_id, pk=endpoint_id)
        return tenant, endpoint

    def patch(self, request, tenant_id, endpoint_id):
        tenant, endpoint = self._get(request, tenant_id, endpoint_id)
        if not _is_admin(request, tenant):
            return _error(
                "TENANT_ADMIN_REQUIRED", "Tenant admin required.", status.HTTP_403_FORBIDDEN
            )
        if "enabled" in request.data:
            endpoint.enabled = bool(request.data["enabled"])
        if "name" in request.data and str(request.data["name"]).strip():
            endpoint.name = str(request.data["name"]).strip()[:120]
        if "webhook_url" in request.data and str(request.data["webhook_url"]).strip():
            endpoint.sealed_url = seal(str(request.data["webhook_url"]).strip(), _SCOPE)
        if "secret" in request.data:
            endpoint.sealed_secret = seal(str(request.data["secret"]).strip(), _SCOPE)
        endpoint.save()
        return Response(_endpoint_data(endpoint))

    def delete(self, request, tenant_id, endpoint_id):
        tenant, endpoint = self._get(request, tenant_id, endpoint_id)
        if not _is_admin(request, tenant):
            return _error(
                "TENANT_ADMIN_REQUIRED", "Tenant admin required.", status.HTTP_403_FORBIDDEN
            )
        endpoint.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationEndpointTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, endpoint_id):
        tenant = _tenant(request, tenant_id)
        endpoint = get_object_or_404(NotificationEndpoint, tenant_id=tenant_id, pk=endpoint_id)
        try:
            result = deliver(
                endpoint,
                "AI Agent Studio test notification",
                f"Tenant {tenant.name} wired this robot at {timezone.now().isoformat()}.",
            )
        except NotificationChannelError as exc:
            return _error(exc.code, str(exc), status.HTTP_400_BAD_REQUEST)
        endpoint.last_sent_at = timezone.now() if result.ok else endpoint.last_sent_at
        endpoint.last_error = "" if result.ok else result.message[:200]
        endpoint.save(update_fields=["last_sent_at", "last_error", "updated_at"])
        record_audit_event(
            request,
            tenant,
            "settings.notification.test",
            "notification_endpoint",
            endpoint.id,
            outcome=AuditEvent.Outcome.SUCCESS if result.ok else AuditEvent.Outcome.FAILURE,
            metadata={"channel": endpoint.channel, "message": result.message},
        )
        return Response(
            {"ok": result.ok, "message": result.message, "endpoint": _endpoint_data(endpoint)},
            status=200 if result.ok else status.HTTP_502_BAD_GATEWAY,
        )
