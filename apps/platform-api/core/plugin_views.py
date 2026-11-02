# ruff: noqa: I001
import re
import uuid

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_node_views import _tenant_and_project, _validate_payload
from core.models import (
    PluginConnection,
    PluginDefinition,
    PluginInstallation,
    PluginTool,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


def _can_manage_project(user, tenant, project):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists() or ProjectMembership.objects.filter(
        project=project,
        user=user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()


def _contains_sensitive_key(value):
    sensitive_parts = (
        "password",
        "secret",
        "token",
        "private_key",
        "kubeconfig",
        "api_key",
        "access_key",
        "authorization",
    )
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in sensitive_parts):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _safe_schema(value):
    if not isinstance(value, dict) or value.get("type") != "object":
        return False
    properties = value.get("properties", {})
    if not isinstance(properties, dict):
        return False
    required = value.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item in properties for item in required
    ):
        return False
    if value.get("additionalProperties", False) is not False:
        return False
    return (
        not _contains_sensitive_key(value)
        and len(str(value).encode("utf-8")) <= 32768
    )


def _plugin_tools(plugin):
    return [
        {
            "id": str(tool.id),
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "required_permissions": tool.required_permissions,
            "risk_level": tool.risk_level,
            "timeout_seconds": tool.timeout_seconds,
            "retry_policy": tool.retry_policy,
            "idempotency_mode": tool.idempotency_mode,
            "enabled": tool.enabled,
        }
        for tool in plugin.tools.all()
    ]


def _plugin_data(plugin):
    return {
        "id": str(plugin.id),
        "plugin_key": plugin.plugin_key,
        "name": plugin.name,
        "description": plugin.description,
        "version": plugin.version,
        "connector_type": plugin.connector_type,
        "maintainer": plugin.maintainer,
        "license": plugin.license,
        "source_url": plugin.source_url,
        "manifest": plugin.manifest,
        "review_status": plugin.review_status,
        "enabled": plugin.enabled,
        "tools": _plugin_tools(plugin),
        "created_at": plugin.created_at,
        "updated_at": plugin.updated_at,
    }


def _installation_data(installation):
    return {
        "id": str(installation.id),
        "tenant_id": str(installation.tenant_id),
        "project_id": str(installation.project_id),
        "plugin_id": str(installation.plugin_id),
        "plugin_name": installation.plugin.name,
        "plugin_key": installation.plugin.plugin_key,
        "plugin_version": installation.plugin_version,
        "status": installation.status,
        "granted_permissions": installation.granted_permissions,
        "config": installation.config,
        "connections": [
            {
                "id": str(connection.id),
                "name": connection.name,
                "auth_type": connection.auth_type,
                "credential_ref": connection.credential_ref,
                "status": connection.status,
                "last_health_check_at": connection.last_health_check_at,
            }
            for connection in installation.connections.all()
        ],
        "created_at": installation.created_at,
        "updated_at": installation.updated_at,
    }


class PluginCatalogListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant_id = request.query_params.get("tenant_id")
        if not tenant_id:
            return Response({"detail": "tenant_id query parameter is required."}, status=400)
        tenant = get_object_or_404(
            Tenant.objects.filter(memberships__user=request.user).distinct(),
            pk=tenant_id,
        )
        plugins = PluginDefinition.objects.filter(tenant=tenant).prefetch_related("tools")
        return Response(
            {
                "count": plugins.count(),
                "results": [_plugin_data(plugin) for plugin in plugins[:100]],
            }
        )

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant_id = request.data.get("tenant_id")
        tenant = get_object_or_404(
            Tenant.objects.filter(memberships__user=request.user).distinct(),
            pk=tenant_id,
        )
        is_admin = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response({"detail": "Tenant owner or admin role required."}, status=403)
        plugin_key = request.data.get("plugin_key")
        version = request.data.get("version")
        connector_type = request.data.get("connector_type")
        tools = request.data.get("tools", [])
        manifest = request.data.get("manifest", {})
        if (
            not isinstance(plugin_key, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", plugin_key)
            or len(plugin_key) > 100
        ):
            return Response({"detail": "plugin_key must be a lowercase slug."}, status=400)
        if not isinstance(version, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}",
            version,
        ):
            return Response({"detail": "A valid plugin version is required."}, status=400)
        if connector_type not in PluginDefinition.ConnectorType.values:
            return Response({"detail": "Unsupported connector_type."}, status=400)
        plugin_name = str(request.data.get("name", plugin_key)).strip()
        source_url = request.data.get("source_url", "")
        if source_url is None:
            source_url = ""
        if not plugin_name or len(plugin_name) > 120:
            return Response({"detail": "Plugin name is required."}, status=400)
        if source_url and (
            not isinstance(source_url, str)
            or not source_url.startswith("https://")
            or len(source_url) > 500
        ):
            return Response({"detail": "source_url must be an HTTPS URL."}, status=400)
        if (
            not isinstance(manifest, dict)
            or not isinstance(tools, list)
            or not 1 <= len(tools) <= 50
        ):
            return Response(
                {"detail": "manifest must be an object and tools must contain 1–50 items."},
                status=400,
            )
        payload_error = _validate_payload({"plugin_manifest": manifest, "tool_definitions": tools})
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        if _contains_sensitive_key(manifest) or _contains_sensitive_key(tools):
            return Response(
                {"detail": "Plugin metadata and schemas cannot contain credential fields."},
                status=400,
            )
        names = set()
        normalized_tools = []
        for item in tools:
            if not isinstance(item, dict):
                return Response({"detail": "Each tool definition must be an object."}, status=400)
            name = item.get("name")
            input_schema = item.get("input_schema")
            output_schema = item.get("output_schema")
            permissions = item.get("required_permissions", [])
            risk_level = item.get("risk_level", PluginTool.RiskLevel.LOW)
            idempotency_mode = item.get(
                "idempotency_mode",
                PluginTool.IdempotencyMode.NONE,
            )
            timeout_seconds = item.get("timeout_seconds", 30)
            retry_policy = item.get("retry_policy", {})
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name)
                or name in names
            ):
                return Response(
                    {"detail": "Tool names must be unique lowercase slugs."},
                    status=400,
                )
            names.add(name)
            if not _safe_schema(input_schema) or not _safe_schema(output_schema):
                return Response(
                    {
                        "detail": (
                            f"Tool {name} input/output schema must be a closed JSON object schema."
                        )
                    },
                    status=400,
                )
            if not isinstance(permissions, list) or not all(
                isinstance(value, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9:._-]{0,127}", value)
                for value in permissions
            ):
                return Response(
                    {"detail": f"Tool {name} has invalid required_permissions."},
                    status=400,
                )
            if risk_level not in PluginTool.RiskLevel.values:
                return Response({"detail": f"Tool {name} has invalid risk_level."}, status=400)
            if idempotency_mode not in PluginTool.IdempotencyMode.values:
                return Response(
                    {"detail": f"Tool {name} has invalid idempotency_mode."},
                    status=400,
                )
            if (
                not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or not 1 <= timeout_seconds <= 300
            ):
                return Response(
                    {"detail": f"Tool {name} timeout_seconds must be 1–300."},
                    status=400,
                )
            if not isinstance(retry_policy, dict) or not set(retry_policy).issubset(
                {"max_attempts", "backoff_seconds"}
            ):
                return Response(
                    {"detail": f"Tool {name} retry_policy has unsupported fields."},
                    status=400,
                )
            max_attempts = retry_policy.get("max_attempts", 1)
            if (
                not isinstance(max_attempts, int)
                or isinstance(max_attempts, bool)
                or not 1 <= max_attempts <= 3
            ):
                return Response({"detail": f"Tool {name} max_attempts must be 1–3."}, status=400)
            backoff_seconds = retry_policy.get("backoff_seconds", 0)
            if (
                not isinstance(backoff_seconds, int)
                or isinstance(backoff_seconds, bool)
                or not 0 <= backoff_seconds <= 30
            ):
                return Response(
                    {"detail": f"Tool {name} backoff_seconds must be 0–30."},
                    status=400,
                )
            if idempotency_mode == PluginTool.IdempotencyMode.NONE and max_attempts != 1:
                return Response(
                    {"detail": f"Non-idempotent tool {name} must use max_attempts=1."},
                    status=400,
                )
            normalized_tools.append(
                {
                    "name": name,
                    "description": str(item.get("description", ""))[:4000],
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "required_permissions": permissions,
                    "risk_level": risk_level,
                    "timeout_seconds": timeout_seconds,
                    "retry_policy": retry_policy,
                    "idempotency_mode": idempotency_mode,
                }
            )
        try:
            with transaction.atomic():
                plugin = PluginDefinition.objects.create(
                    tenant=tenant,
                    created_by=request.user,
                    plugin_key=plugin_key,
                    name=plugin_name,
                    description=str(request.data.get("description", ""))[:4000],
                    version=version,
                    connector_type=connector_type,
                    maintainer=str(request.data.get("maintainer", ""))[:120],
                    license=str(request.data.get("license", ""))[:80],
                    source_url=source_url,
                    manifest=manifest,
                    review_status=PluginDefinition.ReviewStatus.DRAFT,
                    enabled=False,
                )
                PluginTool.objects.bulk_create(
                    [
                        PluginTool(plugin=plugin, **tool)
                        for tool in normalized_tools
                    ]
                )
        except IntegrityError:
            return Response(
                {"detail": "This plugin key/version already exists in the tenant."},
                status=409,
            )
        return Response(_plugin_data(plugin), status=201)


class PluginDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, plugin_id):
        plugin = get_object_or_404(
            PluginDefinition.objects.filter(
                tenant__memberships__user=request.user
            ).prefetch_related("tools"),
            pk=plugin_id,
        )
        return Response(_plugin_data(plugin))


class PluginApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, plugin_id):
        plugin = get_object_or_404(
            PluginDefinition.objects.filter(tenant__memberships__user=request.user),
            pk=plugin_id,
        )
        is_admin = TenantMembership.objects.filter(
            tenant=plugin.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response({"detail": "Tenant owner or admin role required."}, status=403)
        if plugin.review_status == PluginDefinition.ReviewStatus.REJECTED:
            return Response({"detail": "Rejected plugin versions cannot be approved."}, status=409)
        plugin.review_status = PluginDefinition.ReviewStatus.APPROVED
        plugin.enabled = True
        plugin.save(update_fields=["review_status", "enabled", "updated_at"])
        return Response(_plugin_data(plugin))


class PluginInstallationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        installations = PluginInstallation.objects.filter(
            tenant=tenant,
            project=project,
        ).select_related("plugin").prefetch_related("connections")
        return Response(
            {
                "count": installations.count(),
                "results": [_installation_data(item) for item in installations[:100]],
            }
        )

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        if not _can_manage_project(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)
        try:
            plugin_id = uuid.UUID(str(request.data.get("plugin_id")))
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "A valid plugin_id is required."}, status=400)
        plugin = get_object_or_404(
            PluginDefinition.objects.filter(
                tenant=tenant,
                review_status=PluginDefinition.ReviewStatus.APPROVED,
                enabled=True,
            ),
            pk=plugin_id,
        )
        granted_permissions = request.data.get("granted_permissions", [])
        config = request.data.get("config", {})
        if not isinstance(granted_permissions, list) or not all(
            isinstance(value, str) for value in granted_permissions
        ):
            return Response({"detail": "granted_permissions must be a string array."}, status=400)
        declared_permissions = {
            permission
            for tool in plugin.tools.all()
            for permission in tool.required_permissions
        }
        if not set(granted_permissions).issubset(declared_permissions):
            return Response(
                {"detail": "Cannot grant permissions not declared by the plugin."},
                status=400,
            )
        if not isinstance(config, dict):
            return Response({"detail": "config must be a JSON object."}, status=400)
        payload_error = _validate_payload({"config": config})
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        if _contains_sensitive_key(config):
            return Response(
                {"detail": "Plugin configuration cannot contain credential fields."},
                status=400,
            )
        try:
            with transaction.atomic():
                installation, created = PluginInstallation.objects.get_or_create(
                    tenant=tenant,
                    project=project,
                    plugin=plugin,
                    defaults={
                        "plugin_version": plugin.version,
                        "granted_permissions": sorted(set(granted_permissions)),
                        "config": config,
                        "installed_by": request.user,
                        "status": PluginInstallation.Status.INSTALLED,
                    },
                )
                if not created:
                    if installation.plugin_version != plugin.version:
                        return Response(
                            {
                                "detail": (
                                    "Installed plugin version is pinned; uninstall before upgrading."
                                )
                            },
                            status=409,
                        )
                    installation.granted_permissions = sorted(set(granted_permissions))
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
        except IntegrityError:
            return Response(
                {"detail": "Plugin installation conflicts with an existing record."},
                status=409,
            )
        return Response(
            _installation_data(installation),
            status=201 if created else 200,
        )


class PluginInstallationStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, installation_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        installation = get_object_or_404(
            PluginInstallation.objects.filter(tenant__memberships__user=request.user),
            pk=installation_id,
        )
        if not _can_manage_project(
            request.user,
            installation.tenant,
            installation.project,
        ):
            return Response({"detail": "Project editor permission required."}, status=403)
        status_value = request.data.get("status")
        if not isinstance(status_value, str) or status_value not in {
            PluginInstallation.Status.DISABLED,
            PluginInstallation.Status.UNINSTALLED,
            PluginInstallation.Status.INSTALLED,
        }:
            return Response({"detail": "Unsupported installation status."}, status=400)
        installation.status = status_value
        installation.save(update_fields=["status", "updated_at"])
        if status_value == PluginInstallation.Status.UNINSTALLED:
            installation.connections.update(status=PluginConnection.Status.DISABLED)
        return Response(_installation_data(installation))


class PluginConnectionListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, installation_id):
        installation = get_object_or_404(
            PluginInstallation.objects.filter(tenant__memberships__user=request.user),
            pk=installation_id,
        )
        if not ProjectMembership.objects.filter(
            project=installation.project,
            user=request.user,
        ).exists() and not TenantMembership.objects.filter(
            tenant=installation.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists():
            return Response({"detail": "Project membership required."}, status=403)
        return Response(
            {
                "count": installation.connections.count(),
                "results": _installation_data(installation)["connections"],
            }
        )

    def post(self, request, installation_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        installation = get_object_or_404(
            PluginInstallation.objects.filter(tenant__memberships__user=request.user),
            pk=installation_id,
        )
        if not _can_manage_project(
            request.user,
            installation.tenant,
            installation.project,
        ):
            return Response({"detail": "Project editor permission required."}, status=403)
        if installation.status != PluginInstallation.Status.INSTALLED:
            return Response({"detail": "Plugin installation is not active."}, status=409)
        name = request.data.get("name")
        auth_type = request.data.get("auth_type", PluginConnection.AuthType.NONE)
        credential_ref = request.data.get("credential_ref", "")
        config = request.data.get("config", {})
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            return Response({"detail": "Connection name is required."}, status=400)
        if auth_type not in PluginConnection.AuthType.values:
            return Response({"detail": "Unsupported auth_type."}, status=400)
        if auth_type != PluginConnection.AuthType.NONE and (
            not isinstance(credential_ref, str)
            or not re.fullmatch(
                r"[a-z][a-z0-9+.-]*://[A-Za-z0-9._/-]{1,180}",
                credential_ref,
            )
        ):
            return Response(
                {
                    "detail": (
                        "credential_ref must be a non-secret URI such as "
                        "vault://path/to/credential."
                    )
                },
                status=400,
            )
        if auth_type == PluginConnection.AuthType.NONE and credential_ref:
            return Response(
                {"detail": "credential_ref is not allowed for auth_type=none."},
                status=400,
            )
        if not isinstance(config, dict):
            return Response({"detail": "config must be a JSON object."}, status=400)
        payload_error = _validate_payload({"config": config})
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        if _contains_sensitive_key(config):
            return Response(
                {"detail": "Connection configuration cannot contain credential fields."},
                status=400,
            )
        try:
            connection = PluginConnection.objects.create(
                installation=installation,
                name=name.strip(),
                auth_type=auth_type,
                credential_ref=credential_ref,
                config=config,
                status=PluginConnection.Status.PENDING,
            )
        except IntegrityError:
            return Response({"detail": "Connection name already exists."}, status=409)
        return Response(
            {
                "id": str(connection.id),
                "name": connection.name,
                "auth_type": connection.auth_type,
                "credential_ref": connection.credential_ref,
                "status": connection.status,
            },
            status=201,
        )
