# ruff: noqa: I001
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.mcp_tool_sync import sync_mcp_tools
from core.models import AuditEvent, PluginConnection, PluginDefinition, PluginInstallation
from core.plugin_views import _can_manage_project
from core.tool_router import ToolContext, ToolExecutionError


class PluginConnectionToolSyncView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, connection_id):
        connection = get_object_or_404(
            PluginConnection.objects.select_related(
                "installation",
                "installation__tenant",
                "installation__project",
                "installation__plugin",
            ).filter(installation__tenant__memberships__user=request.user),
            pk=connection_id,
        )
        installation = PluginInstallation.objects.select_for_update().get(
            pk=connection.installation_id
        )
        connection = PluginConnection.objects.select_for_update().get(pk=connection.pk)
        plugin = installation.plugin
        if not _can_manage_project(
            request.user,
            installation.tenant,
            installation.project,
        ):
            return Response({"detail": "Project editor permission required."}, status=403)
        if installation.status != PluginInstallation.Status.INSTALLED:
            return Response({"detail": "Plugin installation is not active."}, status=409)
        if plugin.connector_type != PluginDefinition.ConnectorType.MCP:
            return Response({"detail": "Tool discovery requires an MCP connector."}, status=409)
        context = ToolContext(
            tenant_id=str(installation.tenant_id),
            project_id=str(installation.project_id),
            installation_id=str(installation.id),
            plugin_key=plugin.plugin_key,
            plugin_version=plugin.version,
            tool_name="tools/list",
            connection_id=str(connection.id),
            connection_config=dict(connection.config),
            connection_auth_type=connection.auth_type,
            credential_ref=connection.credential_ref,
            plugin_manifest=dict(plugin.manifest) if isinstance(plugin.manifest, dict) else {},
        )
        try:
            summary = sync_mcp_tools(plugin, context)
        except ToolExecutionError as exc:
            record_audit_event(
                request,
                installation.tenant,
                "connector.tools.sync",
                "plugin_connection",
                connection.id,
                project=installation.project,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"plugin_key": plugin.plugin_key, "connection_name": connection.name},
            )
            return Response({"detail": str(exc)}, status=502)
        record_audit_event(
            request,
            installation.tenant,
            "connector.tools.sync",
            "plugin_connection",
            connection.id,
            project=installation.project,
            metadata={
                "plugin_key": plugin.plugin_key,
                "connection_name": connection.name,
                "created": len(summary["created"]),
                "updated": len(summary["updated"]),
                "unchanged": len(summary["unchanged"]),
            },
        )
        return Response(summary)
