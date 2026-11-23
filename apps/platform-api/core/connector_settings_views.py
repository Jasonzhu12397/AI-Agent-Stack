# ruff: noqa: I001
import re
from types import SimpleNamespace

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.data_controls_views import (
    error_response,
    get_tenant_for_user,
    require_admin,
)
from core.http_connector import execute_http_operation
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    AuditEvent,
    PluginConnection,
    PluginDefinition,
    PluginInstallation,
    Project,
    ProjectMembership,
    TenantMembership,
)
from core.tool_router import ToolContext, ToolExecutionError

_CREDENTIAL_REF_PATTERN = re.compile(r"^(env|vault)://[A-Za-z0-9._/-]{1,180}$")
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "private_key",
    "kubeconfig",
    "api_key",
    "access_key",
    "authorization",
    "credential",
)


class ConnectorCheckError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _contains_sensitive_key(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _masked_hint(ref):
    if not ref:
        return ""
    tail = ref[-4:]
    return f"…{tail}" if len(ref) > 4 else "••••"


def _connection_health(installation):
    connections = list(installation.connections.all())
    if not connections:
        return "not_configured"
    statuses = {connection.status for connection in connections}
    if PluginConnection.Status.HEALTHY in statuses:
        return "healthy"
    if PluginConnection.Status.ERROR in statuses:
        return "error"
    return "pending"


def _plugin_installation_payload(installation):
    return {
        "id": str(installation.id),
        "project_id": str(installation.project_id),
        "plugin_id": str(installation.plugin_id),
        "status": installation.status,
        "health": _connection_health(installation),
        "scopes": list(installation.granted_permissions),
    }


def _agent_installation_payload(installation):
    return {
        "id": str(installation.id),
        "project_id": str(installation.project_id),
        "plugin_id": str(installation.plugin_id),
        "status": installation.status,
        "health": "installed"
        if installation.status == AgentPluginInstallation.Status.INSTALLED
        else installation.status,
        "scopes": list(installation.granted_capabilities),
    }


def _catalog_entry(definition, source, installations):
    scopes = sorted({scope for item in installations for scope in item["scopes"]})
    return {
        "slug": definition.plugin_key,
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "source": source,
        "connector_type": getattr(definition, "connector_type", None)
        or getattr(definition, "protocol", None),
        "installed": bool(installations),
        "scopes": scopes,
        "installations": installations,
    }


class ConnectorCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = get_tenant_for_user(request, tenant_id)

        plugin_installations = {
            installation.plugin_id: installation
            for installation in PluginInstallation.objects.filter(tenant=tenant)
            .select_related("plugin")
            .prefetch_related("connections")
        }
        agent_installations = {
            installation.plugin_id: installation
            for installation in AgentPluginInstallation.objects.filter(tenant=tenant)
            .select_related("plugin")
        }

        results = []
        for definition in PluginDefinition.objects.filter(tenant=tenant):
            installations = [
                _plugin_installation_payload(plugin_installations[definition.id])
            ] if definition.id in plugin_installations else []
            source = (
                "mcp"
                if definition.connector_type == PluginDefinition.ConnectorType.MCP
                else "plugin"
            )
            results.append(_catalog_entry(definition, source, installations))

        for definition in AgentPluginDefinition.objects.filter(tenant=tenant):
            installations = [
                _agent_installation_payload(agent_installations[definition.id])
            ] if definition.id in agent_installations else []
            results.append(_catalog_entry(definition, "agent_plugin", installations))

        return Response({"results": results})


class ConnectorInstallView(APIView):
    permission_classes = [IsAuthenticated]

    def _resolve_definition(self, tenant, slug):
        plugin = PluginDefinition.objects.filter(
            tenant=tenant, plugin_key=slug
        ).first()
        if plugin is not None:
            return "plugin", plugin
        agent = AgentPluginDefinition.objects.filter(
            tenant=tenant, plugin_key=slug
        ).first()
        if agent is not None:
            return "agent", agent
        return None, None

    def post(self, request, tenant_id, slug):
        tenant = get_tenant_for_user(request, tenant_id)
        body = request.data if isinstance(request.data, dict) else {}
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )

        source, definition = self._resolve_definition(tenant, slug)
        if definition is None:
            return error_response(
                "CONNECTOR_NOT_FOUND", f"Unknown connector: {slug}", status=404
            )

        project_id = body.get("project_id")
        project = get_object_or_404(Project.objects.filter(tenant=tenant), pk=project_id)
        if not ProjectMembership.objects.filter(
            project=project, user=request.user
        ).exists() and not TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists():
            return error_response(
                "FORBIDDEN", "Project membership required.", status=403
            )

        config = body.get("config", {})
        if not isinstance(config, dict):
            return error_response(
                "CONNECTOR_CONFIG_INVALID", "config must be a JSON object.", status=400
            )
        if _contains_sensitive_key(config):
            return error_response(
                "CONNECTOR_SECRET_NOT_REFERENCE",
                "Configuration must not contain secret values; use a credential reference.",
                status=400,
            )

        if source == "plugin":
            permissions = body.get("granted_permissions", [])
            if not isinstance(permissions, list) or not all(
                isinstance(item, str) for item in permissions
            ):
                return error_response(
                    "CONNECTOR_SCOPES_INVALID",
                    "granted_permissions must be a string array.",
                    status=400,
                )
            with transaction.atomic():
                installation, created = PluginInstallation.objects.get_or_create(
                    tenant=tenant,
                    project=project,
                    plugin=definition,
                    defaults={
                        "plugin_version": definition.version,
                        "granted_permissions": sorted(set(permissions)),
                        "config": config,
                        "installed_by": request.user,
                        "status": PluginInstallation.Status.INSTALLED,
                    },
                )
                if not created:
                    installation.granted_permissions = sorted(set(permissions))
                    installation.config = config
                    installation.status = PluginInstallation.Status.INSTALLED
                    installation.save(
                        update_fields=[
                            "granted_permissions",
                            "config",
                            "status",
                            "updated_at",
                        ]
                    )
            payload = _plugin_installation_payload(installation)
        else:
            capabilities = body.get("granted_capabilities", [])
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                return error_response(
                    "CONNECTOR_SCOPES_INVALID",
                    "granted_capabilities must be a string array.",
                    status=400,
                )
            with transaction.atomic():
                installation, created = AgentPluginInstallation.objects.get_or_create(
                    tenant=tenant,
                    project=project,
                    plugin=definition,
                    defaults={
                        "plugin_version": definition.version,
                        "granted_capabilities": capabilities,
                        "installed_by": request.user,
                        "status": AgentPluginInstallation.Status.INSTALLED,
                    },
                )
            payload = _agent_installation_payload(installation)

        if created:
            record_audit_event(
                request,
                tenant,
                "connector.installation.created",
                "connector_installation",
                installation.id,
                project=project,
                metadata={
                    "connector_slug": definition.plugin_key,
                    "connector_version": installation.plugin_version,
                    "source": source,
                },
            )
        return Response(payload, status=201 if created else 200)


def _resolve_installation(tenant, installation_id):
    plugin_installation = (
        PluginInstallation.objects.select_related("plugin", "project")
        .prefetch_related("connections")
        .filter(tenant=tenant, pk=installation_id)
        .first()
    )
    if plugin_installation is not None:
        return "plugin", plugin_installation, plugin_installation.plugin
    agent_installation = (
        AgentPluginInstallation.objects.select_related("plugin", "project")
        .filter(tenant=tenant, pk=installation_id)
        .first()
    )
    if agent_installation is not None:
        return "agent", agent_installation, agent_installation.plugin
    return None, None, None


class ConnectorInstallationUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, tenant_id, installation_id):
        tenant = get_tenant_for_user(request, tenant_id)
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        source, installation, definition = _resolve_installation(tenant, installation_id)
        if installation is None:
            return error_response(
                "INSTALLATION_NOT_FOUND", "Installation not found for this tenant.", status=404
            )

        body = request.data if isinstance(request.data, dict) else {}
        config = body.get("config")
        if config is not None and not isinstance(config, dict):
            return error_response(
                "CONNECTOR_CONFIG_INVALID", "config must be a JSON object.", status=400
            )
        if config and _contains_sensitive_key(config):
            return error_response(
                "CONNECTOR_SECRET_NOT_REFERENCE",
                "Configuration must not contain secret values; use a credential reference.",
                status=400,
            )

        credential_ref = body.get("credential_ref")
        if credential_ref is not None:
            if not isinstance(credential_ref, str) or not _CREDENTIAL_REF_PATTERN.fullmatch(
                credential_ref
            ):
                return error_response(
                    "CONNECTOR_SECRET_NOT_REFERENCE",
                    "credential_ref must be an env:// or vault:// reference, not a secret value.",
                    status=400,
                )

        if source == "plugin":
            merged_config = dict(installation.config)
            if config:
                merged_config.update(config)
            installation.config = merged_config
            if credential_ref is not None:
                connection, _ = PluginConnection.objects.get_or_create(
                    installation=installation,
                    name="default",
                    defaults={
                        "auth_type": PluginConnection.AuthType.CREDENTIAL_REF,
                        "credential_ref": credential_ref,
                        "status": PluginConnection.Status.PENDING,
                    },
                )
                connection.credential_ref = credential_ref
                connection.auth_type = PluginConnection.AuthType.CREDENTIAL_REF
                connection.save(
                    update_fields=["credential_ref", "auth_type", "updated_at"]
                )
            installation.save(update_fields=["config", "updated_at"])
            configured = bool(
                credential_ref
                or any(connection.credential_ref for connection in installation.connections.all())
            )
            hint = _masked_hint(
                credential_ref
                or next(
                    (connection.credential_ref for connection in installation.connections.all()),
                    "",
                )
            )
        else:
            if credential_ref is not None:
                installation.credential_ref = credential_ref
            installation.save(update_fields=["credential_ref", "updated_at"])
            configured = bool(installation.credential_ref)
            hint = _masked_hint(installation.credential_ref)

        record_audit_event(
            request,
            tenant,
            "connector.installation.updated",
            "connector_installation",
            str(installation.id),
            project=installation.project,
            metadata={"connector_slug": definition.plugin_key, "source": source},
        )
        return Response(
            {
                "id": str(installation.id),
                "configured": configured,
                "masked_hint": hint,
                "config": installation.config if source == "plugin" else {},
                "status": installation.status,
            }
        )


def _run_gateway_check(installation, connection, plugin, *, purpose):
    manifest = dict(plugin.manifest) if isinstance(plugin.manifest, dict) else {}
    transport = manifest.get("http")
    if not isinstance(transport, dict):
        raise ConnectorCheckError(
            "CONNECTOR_MANIFEST_INVALID",
            "Connector manifest does not define an http transport.",
        )
    health_check = transport.get("health_check")
    if not isinstance(health_check, dict):
        raise ConnectorCheckError(
            "CONNECTOR_MANIFEST_INVALID",
            "Connector manifest does not define http.health_check.",
        )
    operations = transport.get("operations")
    if not isinstance(operations, dict):
        raise ConnectorCheckError(
            "CONNECTOR_MANIFEST_INVALID",
            "Connector HTTP operation map is invalid.",
        )
    operations = dict(operations)
    operations["__health_check__"] = health_check
    transport_config = dict(transport)
    transport_config["operations"] = operations
    manifest["http"] = transport_config

    context = ToolContext(
        tenant_id=str(installation.tenant_id),
        project_id=str(installation.project_id),
        installation_id=str(installation.id),
        plugin_key=plugin.plugin_key,
        plugin_version=plugin.version,
        tool_name="__health_check__",
        connection_id=str(connection.id) if connection else None,
        connection_config=dict(connection.config) if connection else {},
        connection_auth_type=connection.auth_type if connection else "none",
        credential_ref=connection.credential_ref if connection else "",
        plugin_manifest=manifest,
    )
    arguments = health_check.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ConnectorCheckError(
            "CONNECTOR_MANIFEST_INVALID", "Health-check arguments must be an object."
        )
    try:
        execute_http_operation(
            arguments,
            context,
            SimpleNamespace(
                name="__health_check__",
                timeout_seconds=15,
                idempotency_mode="read_only",
            ),
        )
    except ToolExecutionError as exc:
        code = (
            "CONNECTOR_HEALTH_CHECK_FAILED"
            if purpose == "health"
            else "CONNECTOR_TEST_FAILED"
        )
        raise ConnectorCheckError(code, str(exc)) from exc


class ConnectorHealthView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, installation_id):
        tenant = get_tenant_for_user(request, tenant_id)
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        source, installation, plugin = _resolve_installation(tenant, installation_id)
        if installation is None:
            return error_response(
                "INSTALLATION_NOT_FOUND", "Installation not found for this tenant.", status=404
            )
        if installation.status != PluginInstallation.Status.INSTALLED:
            return error_response(
                "CONNECTOR_NOT_ACTIVE", "Installation is not active.", status=409
            )
        if source != "plugin":
            return error_response(
                "CONNECTOR_TRANSPORT_UNSUPPORTED",
                "Health checks require an HTTP connector.",
                status=409,
            )
        connection = installation.connections.first()
        if connection is None:
            return error_response(
                "CONNECTOR_NOT_CONFIGURED",
                "Configure a connection before running a health check.",
                status=409,
            )
        try:
            _run_gateway_check(installation, connection, plugin, purpose="health")
        except ConnectorCheckError as exc:
            connection.status = PluginConnection.Status.ERROR
            connection.last_health_check_at = timezone.now()
            connection.save(update_fields=["status", "last_health_check_at", "updated_at"])
            record_audit_event(
                request,
                tenant,
                "connector.health_check.failed",
                "plugin_connection",
                connection.id,
                project=installation.project,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"connector_slug": plugin.plugin_key},
            )
            return error_response(exc.code, exc.message, status=502)

        connection.status = PluginConnection.Status.HEALTHY
        connection.last_health_check_at = timezone.now()
        connection.save(update_fields=["status", "last_health_check_at", "updated_at"])
        record_audit_event(
            request,
            tenant,
            "connector.health_check.succeeded",
            "plugin_connection",
            connection.id,
            project=installation.project,
            metadata={"connector_slug": plugin.plugin_key},
        )
        return Response(_plugin_installation_payload(installation))


class ConnectorTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, installation_id):
        tenant = get_tenant_for_user(request, tenant_id)
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        source, installation, plugin = _resolve_installation(tenant, installation_id)
        if installation is None:
            return error_response(
                "INSTALLATION_NOT_FOUND", "Installation not found for this tenant.", status=404
            )
        if installation.status != PluginInstallation.Status.INSTALLED:
            return error_response(
                "CONNECTOR_NOT_ACTIVE", "Installation is not active.", status=409
            )
        if source != "plugin":
            return error_response(
                "CONNECTOR_TRANSPORT_UNSUPPORTED",
                "Test invocation requires an HTTP connector.",
                status=409,
            )
        connection = installation.connections.first()
        if connection is None:
            return error_response(
                "CONNECTOR_NOT_CONFIGURED",
                "Configure a connection before running a test invocation.",
                status=409,
            )
        try:
            _run_gateway_check(installation, connection, plugin, purpose="test")
        except ConnectorCheckError as exc:
            record_audit_event(
                request,
                tenant,
                "connector.test.failed",
                "connector_installation",
                str(installation.id),
                project=installation.project,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"connector_slug": plugin.plugin_key, "code": exc.code},
            )
            return error_response(exc.code, exc.message, status=502)

        record_audit_event(
            request,
            tenant,
            "connector.test.succeeded",
            "connector_installation",
            str(installation.id),
            project=installation.project,
            metadata={"connector_slug": plugin.plugin_key},
        )
        return Response({"status": "connected", "installation_id": str(installation.id)})
