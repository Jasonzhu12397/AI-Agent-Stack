"""Bind marketplace agent plugin installations onto reusable agent definitions.

The binding layer projects one ``AgentPluginInstallation`` plus one declared
capability onto the existing :class:`AgentDefinition` / :class:`AgentVersion`
pair without adding any table or column: every marketplace-specific detail
lives in the JSON snapshot. The mapping is fully data-driven -- plugin keys,
capability names, schemas and risk levels all come from the stored plugin
manifest, never from a vendor table or a hard-coded endpoint.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import agent_plugin_manifest
from core.audit import record_audit_event
from core.models import (
    AgentDefinition,
    AgentPluginInstallation,
    AgentVersion,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)

MARKETPLACE_AGENT_KIND = "marketplace_agent"

# Binding-level failure codes. Router codes are re-exported here so the
# workflow engine and the API layer have one import surface for the full set
# of marketplace agent failure reasons.
BINDING_INSTALLATION_INACTIVE = "BINDING_INSTALLATION_INACTIVE"
BINDING_CAPABILITY_NOT_DECLARED = "BINDING_CAPABILITY_NOT_DECLARED"
BINDING_VERSION_MISMATCH = "MARKETPLACE_AGENT_VERSION_MISMATCH"

_SLUG_PART = re.compile(r"[^a-z0-9]+")


class AgentBindingError(Exception):
    """A capability could not be bound; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BoundAgent:
    definition: AgentDefinition
    version: AgentVersion
    definition_created: bool
    version_created: bool

    @property
    def snapshot(self) -> dict[str, Any]:
        return self.version.config_snapshot


def _normalize_slug_part(value: str) -> str:
    return _SLUG_PART.sub("-", value.lower()).strip("-")


def _binding_slug(plugin_key: str, capability: str) -> str:
    """Deterministic, stable slug for one (plugin, capability) pair.

    ``AgentDefinition.slug`` is capped at 80 characters. A short hash suffix
    keeps long identifiers unique after truncation while remaining reproducible
    across repeated binds.
    """

    base = f"{_normalize_slug_part(plugin_key)}-{_normalize_slug_part(capability)}"
    digest = hashlib.sha1(f"{plugin_key}:{capability}".encode()).hexdigest()[:6]
    if len(base) > 72:
        base = base[:72].rstrip("-")
    return f"{base}-{digest}"[:80]


def _snapshot(
    installation: AgentPluginInstallation, capability_definition: dict[str, Any]
) -> dict[str, Any]:
    plugin = installation.plugin
    return {
        "kind": MARKETPLACE_AGENT_KIND,
        "plugin_key": plugin.plugin_key,
        "plugin_version": installation.plugin_version,
        "installation_id": str(installation.id),
        "capability": capability_definition["name"],
        "protocol": plugin.protocol,
        "input_schema": capability_definition.get("input_schema") or {},
        "output_schema": capability_definition.get("output_schema") or {},
        "risk_level": capability_definition.get("risk_level", "low"),
    }


def _ensure_active(installation: AgentPluginInstallation) -> None:
    installation.refresh_from_db()
    installation.plugin.refresh_from_db()
    if installation.status != AgentPluginInstallation.Status.INSTALLED:
        raise AgentBindingError(
            BINDING_INSTALLATION_INACTIVE,
            "The agent plugin installation is not installed and cannot be bound.",
        )
    if not installation.plugin.enabled:
        raise AgentBindingError(
            BINDING_INSTALLATION_INACTIVE,
            "The agent plugin is disabled and cannot be bound.",
        )


def _existing_definition(
    project, installation: AgentPluginInstallation, capability_name: str
) -> AgentDefinition | None:
    for definition in AgentDefinition.objects.filter(project=project):
        config = definition.draft_config or {}
        if (
            config.get("kind") == MARKETPLACE_AGENT_KIND
            and config.get("installation_id") == str(installation.id)
            and config.get("capability") == capability_name
        ):
            return definition
    return None


def bind_capability(
    installation: AgentPluginInstallation, capability_name: str, user
) -> BoundAgent:
    """Bind one declared capability of an installation to an agent definition.

    The operation is idempotent: repeated binds with an unchanged plugin
    version return the existing definition and version. A changed plugin
    version adds exactly one new :class:`AgentVersion`; previously published
    versions are left untouched so older workflow references keep resolving
    to their pinned snapshot.
    """

    _ensure_active(installation)
    capability_definition = agent_plugin_manifest.capability_definition(
        installation.plugin, capability_name
    )
    if capability_definition is None:
        raise AgentBindingError(
            BINDING_CAPABILITY_NOT_DECLARED,
            f"The plugin does not declare capability {capability_name!r}.",
        )

    snapshot = _snapshot(installation, capability_definition)
    with transaction.atomic():
        definition = _existing_definition(installation.project, installation, capability_name)
        if definition is None:
            name = f"{installation.plugin.name}: {capability_name}"[:120]
            definition = AgentDefinition(
                tenant=installation.tenant,
                project=installation.project,
                created_by=user,
                name=name,
                slug=_binding_slug(installation.plugin.plugin_key, capability_name),
                description=capability_definition.get("description", "")[:1000],
                draft_config=snapshot,
            )
            definition.save()
            version = AgentVersion.objects.create(
                agent=definition,
                version=1,
                config_snapshot=snapshot,
                created_by=user,
            )
            definition.current_version = 1
            definition.save(update_fields=["current_version", "updated_at"])
            return BoundAgent(definition, version, True, True)

        previous = definition.versions.order_by("-version").first()
        previous_snapshot = previous.config_snapshot if previous else {}
        if previous_snapshot == snapshot:
            # Idempotent repeat: keep the existing version pinned.
            return BoundAgent(definition, previous, False, False)

        next_version = (previous.version if previous else 0) + 1
        version = AgentVersion.objects.create(
            agent=definition,
            version=next_version,
            config_snapshot=snapshot,
            created_by=user,
        )
        definition.draft_config = snapshot
        definition.current_version = next_version
        definition.save(update_fields=["draft_config", "current_version", "updated_at"])
        return BoundAgent(definition, version, False, True)


def bind_installation(
    installation: AgentPluginInstallation, capabilities: list[str] | None, user
) -> list[BoundAgent]:
    """Bind every requested capability (or all declared ones by default)."""

    _ensure_active(installation)
    declared = agent_plugin_manifest.capability_names(installation.plugin)
    if capabilities is None:
        requested = declared
    else:
        requested = list(dict.fromkeys(capabilities))
        unknown = [name for name in requested if name not in declared]
        if unknown:
            raise AgentBindingError(
                BINDING_CAPABILITY_NOT_DECLARED,
                f"The plugin does not declare capabilities: {', '.join(unknown)}.",
            )
    return [bind_capability(installation, name, user) for name in requested]


def _availability(
    installation: AgentPluginInstallation | None, snapshot: dict[str, Any]
) -> tuple[bool, str | None]:
    if installation is None:
        return False, "installation_missing"
    if installation.status == AgentPluginInstallation.Status.UNINSTALLED:
        return False, "installation_uninstalled"
    if installation.status == AgentPluginInstallation.Status.DISABLED:
        return False, "installation_disabled"
    if not installation.plugin.enabled:
        return False, "plugin_disabled"
    if installation.plugin_version != snapshot.get("plugin_version"):
        return False, "version_mismatch"
    granted = installation.granted_capabilities or []
    if granted and snapshot.get("capability") not in granted:
        return False, "capability_not_granted"
    return True, None


def list_marketplace_agents(tenant, project) -> list[dict[str, Any]]:
    """List bound marketplace agents for a project with live availability.

    Uninstalled or disabled installations are still listed, but flagged
    ``available=False`` with a stable reason instead of being hidden or
    reported as usable.
    """

    entries: list[dict[str, Any]] = []
    definitions = AgentDefinition.objects.filter(tenant=tenant, project=project).order_by("name")
    for definition in definitions:
        config = definition.draft_config or {}
        if config.get("kind") != MARKETPLACE_AGENT_KIND:
            continue
        version = definition.versions.filter(version=definition.current_version).first()
        installation = (
            AgentPluginInstallation.objects.filter(pk=config.get("installation_id"))
            .select_related("plugin")
            .first()
        )
        available, reason = _availability(installation, config)
        entries.append(
            {
                "agent_id": str(definition.id),
                "slug": definition.slug,
                "name": definition.name,
                "plugin_key": config.get("plugin_key"),
                "plugin_version": config.get("plugin_version"),
                "capability": config.get("capability"),
                "protocol": config.get("protocol"),
                "risk_level": config.get("risk_level"),
                "agent_version": version.version if version else 0,
                "agent_version_id": str(version.id) if version else None,
                "installation_id": config.get("installation_id"),
                "installation_status": installation.status if installation else None,
                "available": available,
                "availability_reason": reason,
            }
        )
    return entries


def _bound_entry(bound: BoundAgent) -> dict[str, Any]:
    snapshot = bound.snapshot
    return {
        "definition_id": str(bound.definition.id),
        "slug": bound.definition.slug,
        "name": bound.definition.name,
        "agent_version": bound.version.version,
        "agent_version_id": str(bound.version.id),
        "capability": snapshot.get("capability"),
        "plugin_key": snapshot.get("plugin_key"),
        "plugin_version": snapshot.get("plugin_version"),
        "definition_created": bound.definition_created,
        "version_created": bound.version_created,
    }


def _tenant_for_user(request, tenant_id) -> Tenant:
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def _project_for_user(request, tenant: Tenant, project_id) -> Project:
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    queryset = Project.objects.filter(tenant=tenant)
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


def _can_manage_project(user, tenant: Tenant, project: Project) -> bool:
    return (
        TenantMembership.objects.filter(
            tenant=tenant,
            user=user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        or ProjectMembership.objects.filter(
            project=project,
            user=user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
    )


def _installation_for_user(request, installation_id) -> AgentPluginInstallation:
    return get_object_or_404(
        AgentPluginInstallation.objects.select_related("tenant", "project", "plugin").filter(
            tenant__memberships__user=request.user
        ),
        pk=installation_id,
    )


class PluginAgentBindView(APIView):
    """Bind (or refresh) marketplace agents for one plugin installation."""

    permission_classes = [IsAuthenticated]

    def post(self, request, installation_id):
        installation = _installation_for_user(request, installation_id)
        if not _can_manage_project(request.user, installation.tenant, installation.project):
            return Response(
                {"detail": "Project editor permission required."},
                status=403,
            )
        capabilities = request.data.get("capabilities")
        if capabilities is not None and (
            not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
        ):
            return Response(
                {"detail": "capabilities must be an array of capability names."},
                status=400,
            )
        try:
            bound = bind_installation(installation, capabilities, request.user)
        except AgentBindingError as exc:
            return Response({"detail": str(exc), "code": exc.code}, status=400)

        for item in bound:
            record_audit_event(
                request,
                installation.tenant,
                "agent_plugin.agent_bound",
                "agent_definition",
                item.definition.id,
                project=installation.project,
                metadata={
                    "plugin_key": item.snapshot.get("plugin_key"),
                    "capability": item.snapshot.get("capability"),
                    "plugin_version": item.snapshot.get("plugin_version"),
                    "version_created": item.version_created,
                },
            )
        return Response({"bound": [_bound_entry(item) for item in bound]}, status=200)


class MarketplaceAgentListView(APIView):
    """List bound marketplace agents and their live availability."""

    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = _tenant_for_user(request, tenant_id)
        project = _project_for_user(request, tenant, project_id)
        return Response({"agents": list_marketplace_agents(tenant, project)})
