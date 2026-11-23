# ruff: noqa: I001
import ipaddress
import re
from urllib.parse import urlsplit

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import agent_plugin_manifest
from core.audit import record_audit_event
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


_CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_PLUGIN_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "private_key",
    "api_key",
    "access_key",
    "authorization",
    "kubeconfig",
)


def _tenant_for_user(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def _project_for_user(request, tenant, project_id):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    queryset = Project.objects.filter(tenant=tenant)
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


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


def _is_tenant_admin(user, tenant):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()


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


def _validate_endpoint_url(value):
    if not isinstance(value, str) or len(value) > 500:
        return "endpoint_url must be a valid HTTPS URL."
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return "endpoint_url must be a valid HTTPS URL."
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        return "Agent endpoints must use HTTPS on the default port without URL credentials."
    normalized_host = host.rstrip(".").lower()
    if (
        normalized_host in {"localhost", "metadata.google.internal"}
        or normalized_host.endswith((".localhost", ".local", ".internal", ".test"))
    ):
        return "Agent endpoint host is not permitted."
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        return "Agent endpoints must use a DNS hostname, not an IP address."
    if "." not in normalized_host or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in normalized_host.split(".")
    ):
        return "Agent endpoint hostname is invalid."
    return None


def _validate_capabilities(value):
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(
            not isinstance(item, str) or not _CAPABILITY_PATTERN.fullmatch(item)
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        return "capabilities must be a unique array of up to 32 capability identifiers."
    return None


def _validate_schema(value, field_name):
    if (
        not isinstance(value, dict)
        or len(str(value).encode("utf-8")) > 32768
        or _contains_sensitive_key(value)
    ):
        return f"{field_name} must be a non-sensitive JSON object no larger than 32 KiB."
    return None


def _definition_data(plugin):
    return {
        "id": str(plugin.id),
        "tenant_id": str(plugin.tenant_id),
        "plugin_key": plugin.plugin_key,
        "name": plugin.name,
        "description": plugin.description,
        "version": plugin.version,
        "protocol": plugin.protocol,
        "endpoint_url": plugin.endpoint_url,
        "auth_required": plugin.auth_required,
        "capabilities": plugin.capabilities,
        "manifest": plugin.manifest,
        "input_schema": plugin.input_schema,
        "output_schema": plugin.output_schema,
        "timeout_seconds": plugin.timeout_seconds,
        "max_concurrency": plugin.max_concurrency,
        "enabled": plugin.enabled,
        "created_at": plugin.created_at,
        "updated_at": plugin.updated_at,
    }


def _installation_data(installation):
    return {
        "id": str(installation.id),
        "tenant_id": str(installation.tenant_id),
        "project_id": str(installation.project_id),
        "plugin": _definition_data(installation.plugin),
        "plugin_version": installation.plugin_version,
        "status": installation.status,
        "granted_capabilities": installation.granted_capabilities,
        "credential_configured": bool(installation.credential_ref),
        "created_at": installation.created_at,
        "updated_at": installation.updated_at,
    }


class AgentPluginCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = _tenant_for_user(request, tenant_id)
        plugins = AgentPluginDefinition.objects.filter(tenant=tenant).order_by(
            "name", "-created_at"
        )
        return Response([_definition_data(plugin) for plugin in plugins])

    def post(self, request, tenant_id):
        tenant = _tenant_for_user(request, tenant_id)
        if not _is_tenant_admin(request.user, tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)

        data = request.data
        required_strings = ("plugin_key", "name", "version", "protocol", "endpoint_url")
        if any(
            not isinstance(data.get(field), str) or not data[field].strip()
            for field in required_strings
        ):
            return Response(
                {
                    "detail": (
                        "plugin_key, name, version, protocol and endpoint_url are required strings."
                    )
                },
                status=400,
            )
        if len(data["name"]) > 120 or len(data["version"]) > 40 or len(data["plugin_key"]) > 100:
            return Response(
                {"detail": "Agent plugin identity fields exceed their maximum lengths."},
                status=400,
            )
        if not _PLUGIN_KEY_PATTERN.fullmatch(data["plugin_key"]):
            return Response(
                {"detail": "plugin_key must be a lowercase hyphenated identifier."},
                status=400,
            )
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 4000:
            return Response(
                {"detail": "description must be a string no longer than 4,000 characters."},
                status=400,
            )
        if data["protocol"] not in AgentPluginDefinition.Protocol.values:
            return Response({"detail": "Unsupported agent plugin protocol."}, status=400)

        endpoint_error = _validate_endpoint_url(data["endpoint_url"])
        if endpoint_error:
            return Response({"detail": endpoint_error}, status=400)
        manifest = data.get("manifest", {})
        if manifest is None:
            manifest = {}
        if not isinstance(manifest, dict) or len(str(manifest).encode("utf-8")) > (
            agent_plugin_manifest.MAX_MANIFEST_BYTES
        ):
            return Response(
                {"detail": "manifest must be a JSON object no larger than 32 KiB."},
                status=400,
            )
        if _contains_sensitive_key(manifest):
            return Response(
                {"detail": "manifest must not contain credential-like field names."},
                status=400,
            )
        manifest_error = agent_plugin_manifest.validate_manifest(manifest, data["protocol"])
        if manifest_error:
            return Response({"detail": manifest_error}, status=400)
        manifest_names = agent_plugin_manifest.capability_names_from_manifest(manifest)
        if manifest_names:
            capabilities = manifest_names
        else:
            capabilities = data.get("capabilities", [])
            capability_error = _validate_capabilities(capabilities)
            if capability_error:
                return Response({"detail": capability_error}, status=400)
        input_schema = data.get("input_schema", {})
        output_schema = data.get("output_schema", {})
        schemas = ((input_schema, "input_schema"), (output_schema, "output_schema"))
        for schema, field_name in schemas:
            schema_error = _validate_schema(schema, field_name)
            if schema_error:
                return Response({"detail": schema_error}, status=400)

        auth_required = data.get("auth_required", True)
        timeout_seconds = data.get("timeout_seconds", 60)
        max_concurrency = data.get("max_concurrency", 4)
        if not isinstance(auth_required, bool):
            return Response({"detail": "auth_required must be a boolean."}, status=400)
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 300
        ):
            return Response(
                {"detail": "timeout_seconds must be an integer between 1 and 300."},
                status=400,
            )
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= 64
        ):
            return Response(
                {"detail": "max_concurrency must be an integer between 1 and 64."},
                status=400,
            )

        try:
            with transaction.atomic():
                plugin = AgentPluginDefinition.objects.create(
                    tenant=tenant,
                    created_by=request.user,
                    plugin_key=data["plugin_key"],
                    name=data["name"],
                    description=description,
                    version=data["version"],
                    protocol=data["protocol"],
                    endpoint_url=data["endpoint_url"],
                    auth_required=auth_required,
                    capabilities=capabilities,
                    manifest=manifest,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                )
        except IntegrityError:
            return Response(
                {"detail": "An agent plugin with this key and version already exists."},
                status=409,
            )
        record_audit_event(
            request,
            tenant,
            "agent_plugin.registered",
            "agent_plugin_definition",
            plugin.id,
            metadata={
                "plugin_key": plugin.plugin_key,
                "version": plugin.version,
                "protocol": plugin.protocol,
            },
        )
        return Response(_definition_data(plugin), status=201)


class AgentPluginDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, plugin_id):
        plugin = get_object_or_404(
            AgentPluginDefinition.objects.filter(tenant__memberships__user=request.user),
            pk=plugin_id,
        )
        return Response(_definition_data(plugin))

    def post(self, request, plugin_id):
        plugin = get_object_or_404(
            AgentPluginDefinition.objects.filter(tenant__memberships__user=request.user),
            pk=plugin_id,
        )
        if not _is_tenant_admin(request.user, plugin.tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response({"detail": "enabled must be a boolean."}, status=400)
        plugin.enabled = enabled
        plugin.save(update_fields=["enabled", "updated_at"])
        record_audit_event(
            request,
            plugin.tenant,
            "agent_plugin.enabled_changed",
            "agent_plugin_definition",
            plugin.id,
            metadata={"plugin_key": plugin.plugin_key, "enabled": enabled},
        )
        return Response(_definition_data(plugin))


class AgentPluginInstallationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = _tenant_for_user(request, tenant_id)
        project = _project_for_user(request, tenant, project_id)
        installations = (
            AgentPluginInstallation.objects.filter(tenant=tenant, project=project)
            .select_related("plugin")
            .order_by("plugin__name")
        )
        return Response([_installation_data(item) for item in installations])

    def post(self, request, tenant_id, project_id):
        tenant = _tenant_for_user(request, tenant_id)
        project = _project_for_user(request, tenant, project_id)
        if not _can_manage_project(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)

        plugin_id = request.data.get("plugin_id")
        if not isinstance(plugin_id, str):
            return Response({"detail": "plugin_id is required."}, status=400)
        plugin = get_object_or_404(
            AgentPluginDefinition.objects.filter(tenant=tenant, enabled=True),
            pk=plugin_id,
        )
        granted = request.data.get("granted_capabilities", [])
        capability_error = _validate_capabilities(granted)
        if capability_error:
            return Response({"detail": capability_error}, status=400)
        if not set(granted).issubset(set(plugin.capabilities)):
            return Response(
                {"detail": "Granted capabilities must be declared by the agent plugin."},
                status=400,
            )

        credential_ref = request.data.get("credential_ref", "")
        if not isinstance(credential_ref, str) or (
            credential_ref and not _CREDENTIAL_REF_PATTERN.fullmatch(credential_ref)
        ):
            return Response(
                {
                    "detail": (
                        "credential_ref must be a valid secret reference, not a credential value."
                    )
                },
                status=400,
            )
        if plugin.auth_required and not credential_ref:
            return Response(
                {"detail": "A credential reference is required for this agent plugin."},
                status=400,
            )
        if not plugin.auth_required and credential_ref:
            return Response(
                {"detail": "This agent plugin does not accept a credential reference."},
                status=400,
            )

        installation, created = AgentPluginInstallation.objects.get_or_create(
            tenant=tenant,
            project=project,
            plugin=plugin,
            defaults={
                "plugin_version": plugin.version,
                "granted_capabilities": granted,
                "credential_ref": credential_ref,
                "installed_by": request.user,
            },
        )
        if not created:
            return Response(
                {"detail": "Agent plugin is already installed in this project."},
                status=409,
            )
        record_audit_event(
            request,
            tenant,
            "agent_plugin.installed",
            "agent_plugin_installation",
            installation.id,
            project=project,
            metadata={"plugin_key": plugin.plugin_key, "granted_capabilities": granted},
        )
        return Response(_installation_data(installation), status=201)


class AgentPluginInstallationStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, installation_id):
        installation = get_object_or_404(
            AgentPluginInstallation.objects.select_related("tenant", "project", "plugin").filter(
                tenant__memberships__user=request.user,
            ),
            pk=installation_id,
        )
        if not _can_manage_project(request.user, installation.tenant, installation.project):
            return Response({"detail": "Project editor permission required."}, status=403)
        status_value = request.data.get("status")
        if status_value not in {
            AgentPluginInstallation.Status.INSTALLED,
            AgentPluginInstallation.Status.DISABLED,
            AgentPluginInstallation.Status.UNINSTALLED,
        }:
            return Response({"detail": "Unsupported agent plugin installation status."}, status=400)
        if (
            status_value == AgentPluginInstallation.Status.INSTALLED
            and not installation.plugin.enabled
        ):
            return Response({"detail": "Disabled agent plugins cannot be reactivated."}, status=409)
        previous_status = installation.status
        installation.status = status_value
        installation.save(update_fields=["status", "updated_at"])
        record_audit_event(
            request,
            installation.tenant,
            "agent_plugin.installation_status_changed",
            "agent_plugin_installation",
            installation.id,
            project=installation.project,
            metadata={
                "plugin_key": installation.plugin.plugin_key,
                "previous_status": previous_status,
                "status": status_value,
            },
        )
        return Response(_installation_data(installation))
