# ruff: noqa: I001
"""Read-only marketplace catalog for browsing registered agent plugins.

The catalog aggregates the ``AgentPluginDefinition`` records visible to the
requesting tenant, collapses entries that share a ``plugin_key`` down to the
highest declared version, and annotates each entry with the installation state
in the requested project. The endpoints are pure GET handlers: they never
write to the database and never trigger remote discovery or synchronization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import agent_plugin_manifest
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    Tenant,
    TenantMembership,
)


NOT_INSTALLED = "not_installed"

_TENANT_ADMIN_ROLES = [TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN]


def _version_key(version: str) -> tuple:
    """Semver-like ordering, kept identical to the capability router.

    Numeric segments sort ahead of text segments; segments are compared in
    order so ``1.10.0`` outranks ``1.9.0``.
    """

    segments = str(version or "").split(".")
    key: list[tuple[int, int, str]] = []
    for segment in segments:
        if segment.isdigit():
            key.append((0, int(segment), ""))
        else:
            key.append((1, 0, segment))
    return tuple(key)


def resolve_tenant(user, tenant_id: str) -> Tenant | None:
    """Return the tenant only when the user holds any membership in it."""

    return Tenant.objects.filter(memberships__user=user).distinct().filter(pk=tenant_id).first()


def resolve_project(user, tenant: Tenant, project_id: str | None) -> Project | None:
    """Resolve a project in the tenant, honoring project-level membership.

    Tenant owners and admins see every project; other members only see
    projects they belong to.
    """

    if not project_id:
        return None
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=_TENANT_ADMIN_ROLES,
    ).exists()
    queryset = Project.objects.filter(tenant=tenant)
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=user)
    return queryset.distinct().filter(pk=project_id).first()


def _capability_brief(plugin: AgentPluginDefinition) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for declaration in agent_plugin_manifest.declared_capabilities(plugin):
        name = declaration.get("name")
        if not isinstance(name, str):
            continue
        description = declaration.get("description", "")
        entries.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "risk_level": declaration.get("risk_level", "low"),
            }
        )
    return entries


def _capability_detail(plugin: AgentPluginDefinition) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for declaration in agent_plugin_manifest.declared_capabilities(plugin):
        name = declaration.get("name")
        if not isinstance(name, str):
            continue
        entries.append(
            {
                "name": name,
                "description": declaration.get("description", ""),
                "risk_level": declaration.get("risk_level", "low"),
                "timeout_seconds": declaration.get("timeout_seconds", 30),
                "input_schema": declaration.get("input_schema", {}),
                "output_schema": declaration.get("output_schema", {}),
            }
        )
    return entries


def _is_newer(candidate: AgentPluginDefinition, current: AgentPluginDefinition) -> bool:
    candidate_key = (_version_key(candidate.version), candidate.created_at)
    current_key = (_version_key(current.version), current.created_at)
    return candidate_key > current_key


def _latest_definitions(tenant: Tenant) -> list[AgentPluginDefinition]:
    by_key: dict[str, AgentPluginDefinition] = {}
    for plugin in AgentPluginDefinition.objects.filter(tenant=tenant):
        current = by_key.get(plugin.plugin_key)
        if current is None or _is_newer(plugin, current):
            by_key[plugin.plugin_key] = plugin
    return sorted(
        by_key.values(),
        key=lambda plugin: (plugin.name.lower(), plugin.plugin_key),
    )


def _installation_map(
    tenant: Tenant,
    project: Project | None,
    plugins: list[AgentPluginDefinition],
) -> dict[Any, AgentPluginInstallation]:
    if project is None or not plugins:
        return {}
    installations = AgentPluginInstallation.objects.filter(
        tenant=tenant,
        project=project,
        plugin__in=plugins,
    ).select_related("plugin")
    return {installation.plugin_id: installation for installation in installations}


def _entry(
    plugin: AgentPluginDefinition,
    installation: AgentPluginInstallation | None,
    project: Project | None,
) -> dict[str, Any]:
    if installation is None:
        installation_status = NOT_INSTALLED
        installation_id: str | None = None
        installed = False
    else:
        installation_status = installation.status
        installation_id = str(installation.id)
        installed = installation.status != AgentPluginInstallation.Status.UNINSTALLED
    return {
        "plugin_key": plugin.plugin_key,
        "name": plugin.name,
        "description": plugin.description,
        "version": plugin.version,
        "protocol": plugin.protocol,
        "enabled": plugin.enabled,
        "auth_required": plugin.auth_required,
        "capabilities": _capability_brief(plugin),
        "installed": installed,
        "installation_status": installation_status,
        "installation_id": installation_id,
        "project_id": str(project.id) if project is not None else None,
    }


def _matches_capability(plugin: AgentPluginDefinition, entry: dict[str, Any], needle: str) -> bool:
    lowered = needle.lower()
    if any(lowered in cap["name"].lower() for cap in entry["capabilities"]):
        return True
    return lowered in plugin.name.lower() or lowered in plugin.description.lower()


@dataclass(frozen=True)
class CatalogFilters:
    capability: str | None = None
    protocol: str | None = None
    installed: bool | None = None


def _parse_filters(query_params) -> CatalogFilters | Response:
    capability = query_params.get("capability")
    if capability is not None:
        capability = capability.strip() or None

    protocol = query_params.get("protocol")
    if protocol is not None:
        protocol = protocol.strip()
        if protocol not in AgentPluginDefinition.Protocol.values:
            return Response({"detail": "Unsupported protocol filter."}, status=400)

    installed_raw = query_params.get("installed")
    installed: bool | None = None
    if installed_raw is not None:
        normalized = installed_raw.strip().lower()
        if normalized == "true":
            installed = True
        elif normalized == "false":
            installed = False
        else:
            return Response(
                {"detail": "installed filter must be true or false."},
                status=400,
            )

    return CatalogFilters(capability=capability, protocol=protocol, installed=installed)


def build_catalog(
    tenant: Tenant,
    project: Project | None,
    filters: CatalogFilters,
) -> list[dict[str, Any]]:
    plugins = _latest_definitions(tenant)
    installation_map = _installation_map(tenant, project, plugins)
    entries: list[dict[str, Any]] = []
    for plugin in plugins:
        entry = _entry(plugin, installation_map.get(plugin.id), project)
        if filters.protocol is not None and plugin.protocol != filters.protocol:
            continue
        if filters.installed is not None and entry["installed"] != filters.installed:
            continue
        if filters.capability is not None and not _matches_capability(
            plugin, entry, filters.capability
        ):
            continue
        entries.append(entry)
    return entries


def _resolve_scope(request, tenant_id: str):
    tenant = resolve_tenant(request.user, tenant_id)
    if tenant is None:
        raise Http404("Tenant does not exist.")
    project_id = request.query_params.get("project_id")
    project = resolve_project(request.user, tenant, project_id) if project_id else None
    if project_id and project is None:
        raise Http404("Project does not exist.")
    return tenant, project


class AgentMarketplaceCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant, project = _resolve_scope(request, tenant_id)
        parsed = _parse_filters(request.query_params)
        if isinstance(parsed, Response):
            return parsed
        entries = build_catalog(tenant, project, parsed)
        return Response({"entries": entries, "count": len(entries)})


class AgentMarketplaceCatalogDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, plugin_key):
        tenant, project = _resolve_scope(request, tenant_id)
        plugins = _latest_definitions(tenant)
        plugin = next(
            (candidate for candidate in plugins if candidate.plugin_key == plugin_key),
            None,
        )
        if plugin is None:
            raise Http404("Agent plugin does not exist in the marketplace catalog.")
        installation_map = _installation_map(tenant, project, [plugin])
        entry = _entry(plugin, installation_map.get(plugin.id), project)
        entry["capabilities"] = _capability_detail(plugin)
        entry["manifest"] = plugin.manifest
        entry["input_schema"] = plugin.input_schema
        entry["output_schema"] = plugin.output_schema
        return Response(entry)
