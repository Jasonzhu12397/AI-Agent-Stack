"""Deterministic capability resolution and invocation for marketplace agents.

This module is the in-process entry point that turns a project-scoped capability
name into an active installation, validates the call against the declared schema,
dispatches through the registered protocol adapter and records a bounded
invocation outcome. Resolution always reads the database: an uninstall or
disable becomes visible immediately without relying on a warm cache.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from core import agent_plugin_manifest
from core.http_connector import execute_http_operation
from core.models import (
    AgentCapabilityInvocation,
    AgentPluginDefinition,
    AgentPluginInstallation,
)
from core.tool_router import (
    ToolApprovalRequired,
    ToolContext,
    ToolExecutionError,
    validate_schema,
)

CAPABILITY_NOT_DECLARED = "CAPABILITY_NOT_DECLARED"
CAPABILITY_NOT_INSTALLED = "CAPABILITY_NOT_INSTALLED"
CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
CAPABILITY_PROTOCOL_UNSUPPORTED = "CAPABILITY_PROTOCOL_UNSUPPORTED"
CAPABILITY_AUTH_REQUIRED = "CAPABILITY_AUTH_REQUIRED"
CAPABILITY_UNINSTALLED_IN_FLIGHT = "CAPABILITY_UNINSTALLED_IN_FLIGHT"
CAPABILITY_INVOCATION_FAILED = "CAPABILITY_INVOCATION_FAILED"

_INSTALLED = AgentPluginInstallation.Status.INSTALLED
_DISABLED = AgentPluginInstallation.Status.DISABLED
_UNINSTALLED = AgentPluginInstallation.Status.UNINSTALLED


class CapabilityResolutionError(Exception):
    """A capability could not be resolved or invoked; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedCapability:
    installation: AgentPluginInstallation
    plugin: AgentPluginDefinition
    definition: dict[str, Any]
    capability_name: str


def _version_key(version: str) -> tuple:
    """Reproducible semver-like ordering: numeric segments first, text segments last."""
    segments = str(version or "").split(".")
    key: list[tuple[int, int, str]] = []
    for segment in segments:
        if segment.isdigit():
            key.append((0, int(segment), ""))
        else:
            key.append((1, 0, segment))
    return tuple(key)


def _is_active(installation: AgentPluginInstallation) -> bool:
    return installation.status == _INSTALLED and installation.plugin.enabled


def _diagnose(installations: list[AgentPluginInstallation], capability_name: str) -> None:
    """Raise the most specific resolution error for the matched-but-inactive cases."""
    for installation in installations:
        definition = agent_plugin_manifest.capability_definition(
            installation.plugin, capability_name
        )
        if definition is None:
            continue
        if installation.status == _UNINSTALLED:
            raise CapabilityResolutionError(
                CAPABILITY_NOT_INSTALLED,
                "The agent plugin installation was uninstalled.",
            )
        if installation.status == _DISABLED or not installation.plugin.enabled:
            raise CapabilityResolutionError(
                CAPABILITY_DISABLED,
                "The agent plugin installation is disabled.",
            )
    raise CapabilityResolutionError(
        CAPABILITY_NOT_INSTALLED,
        "The capability is not installed in this project.",
    )


def resolve_capability(
    tenant_id: str,
    project_id: str,
    capability_name: str,
) -> ResolvedCapability:
    """Resolve an active, declared capability for the project.

    Never falls back to an empty implementation or ``None``: every failure path
    raises :class:`CapabilityResolutionError` with a stable error code.
    """
    installations = list(
        AgentPluginInstallation.objects.filter(
            tenant_id=tenant_id,
            project_id=project_id,
        ).select_related("plugin")
    )
    matched: list[tuple[AgentPluginInstallation, dict[str, Any]]] = []
    for installation in installations:
        definition = agent_plugin_manifest.capability_definition(
            installation.plugin, capability_name
        )
        if definition is not None:
            matched.append((installation, definition))

    if not matched:
        declared_in_tenant = AgentPluginDefinition.objects.filter(tenant_id=tenant_id)
        if not any(
            agent_plugin_manifest.capability_definition(plugin, capability_name)
            for plugin in declared_in_tenant
        ):
            raise CapabilityResolutionError(
                CAPABILITY_NOT_DECLARED,
                "The capability is not declared by any installed agent plugin.",
            )
        _diagnose(installations, capability_name)

    active = [
        (installation, definition)
        for installation, definition in matched
        if _is_active(installation)
    ]
    if not active:
        _diagnose([installation for installation, _ in matched], capability_name)

    # Deterministic tie-break: newest version wins, then most recent install,
    # then the installation id lexicographically. Stable sorts keep this reproducible.
    ordered = sorted(active, key=lambda pair: pair[0].id)
    ordered.sort(key=lambda pair: pair[0].created_at, reverse=True)
    ordered.sort(key=lambda pair: _version_key(pair[0].plugin.version), reverse=True)
    installation, definition = ordered[0]
    return ResolvedCapability(
        installation=installation,
        plugin=installation.plugin,
        definition=definition,
        capability_name=capability_name,
    )


def _record_invocation(
    installation: AgentPluginInstallation,
    capability: str,
    protocol: str,
    status: str,
    error_code: str,
    latency_ms: int,
) -> None:
    # Arguments and response bodies are intentionally never persisted.
    AgentCapabilityInvocation.objects.create(
        installation=installation,
        capability=capability,
        protocol=protocol,
        status=status,
        error_code=error_code,
        latency_ms=max(latency_ms, 0),
    )


def _http_adapter(
    installation: AgentPluginInstallation,
    definition: dict[str, Any],
    arguments: dict[str, Any],
    approved: bool,
) -> dict[str, Any]:
    plugin = installation.plugin
    capability_name = definition["name"]
    tool = SimpleNamespace(
        name=capability_name,
        timeout_seconds=definition.get("timeout_seconds", plugin.timeout_seconds),
        risk_level=definition.get("risk_level", "low"),
        idempotency_mode="read_only",
        enabled=True,
    )
    context = ToolContext(
        tenant_id=str(installation.tenant_id),
        project_id=str(installation.project_id),
        installation_id=str(installation.id),
        plugin_key=plugin.plugin_key,
        plugin_version=plugin.version,
        tool_name=capability_name,
        connection_id=None,
        connection_config={},
        connection_auth_type="none" if not plugin.auth_required else "credential",
        credential_ref=installation.credential_ref,
        plugin_manifest=dict(plugin.manifest or {}),
    )
    return execute_http_operation(arguments, context, tool)


# Protocol adapters are registered here; mcp/a2a adapters are intentionally
# absent in this batch and surface CAPABILITY_PROTOCOL_UNSUPPORTED instead.
_PROTOCOL_ADAPTERS: dict[str, Callable[..., dict[str, Any]]] = {
    AgentPluginDefinition.Protocol.HTTP_JSON: _http_adapter,
}

# Process-local invalidation hooks run when an installation leaves the active
# state. Connectors may register a hook to drop bound handlers from their router.
_INVALIDATION_HOOKS: list[Callable[[AgentPluginInstallation], None]] = []


def register_protocol_adapter(protocol: str, adapter: Callable[..., dict[str, Any]]) -> None:
    """Register an adapter for a protocol, replacing any previous entry."""
    _PROTOCOL_ADAPTERS[protocol] = adapter


def register_invalidation_hook(hook: Callable[[AgentPluginInstallation], None]) -> None:
    """Register a hook invoked when an installation is invalidated."""
    _INVALIDATION_HOOKS.append(hook)


def invalidate_installation(installation: AgentPluginInstallation) -> None:
    """Run registered invalidation hooks for an installation.

    Hook failures are isolated so one broken binder cannot block the status
    transition; resolution itself already reads the database on every call.
    """
    for hook in list(_INVALIDATION_HOOKS):
        try:
            hook(installation)
        except Exception:
            continue


def _ensure_active(installation: AgentPluginInstallation, in_flight: bool) -> None:
    installation.refresh_from_db()
    installation.plugin.refresh_from_db()
    if installation.status == _UNINSTALLED:
        code = (
            CAPABILITY_UNINSTALLED_IN_FLIGHT if in_flight else CAPABILITY_NOT_INSTALLED
        )
        raise CapabilityResolutionError(code, "The agent plugin installation was uninstalled.")
    if installation.status == _DISABLED or not installation.plugin.enabled:
        code = (
            CAPABILITY_UNINSTALLED_IN_FLIGHT if in_flight else CAPABILITY_DISABLED
        )
        raise CapabilityResolutionError(code, "The agent plugin installation is disabled.")


def invoke_capability(
    tenant_id: str,
    project_id: str,
    capability_name: str,
    arguments: dict[str, Any],
    approved: bool = False,
) -> dict[str, Any]:
    """Resolve, validate and invoke a capability through its protocol adapter."""
    resolved = resolve_capability(tenant_id, project_id, capability_name)
    installation = resolved.installation
    plugin = resolved.plugin
    definition = resolved.definition

    # Re-check the installation state immediately before dispatch.
    _ensure_active(installation, in_flight=False)

    granted = installation.granted_capabilities or []
    if granted and capability_name not in granted:
        raise CapabilityResolutionError(
            CAPABILITY_NOT_DECLARED,
            "The capability has not been granted to this installation.",
        )

    adapter = _PROTOCOL_ADAPTERS.get(plugin.protocol)
    if adapter is None:
        raise CapabilityResolutionError(
            CAPABILITY_PROTOCOL_UNSUPPORTED,
            f"Protocol {plugin.protocol!r} has no invocation adapter.",
        )
    if plugin.auth_required and not installation.credential_ref:
        raise CapabilityResolutionError(
            CAPABILITY_AUTH_REQUIRED,
            "A credential reference is required for this agent plugin.",
        )

    risk_level = definition.get("risk_level", "low")
    input_schema = definition.get("input_schema") or {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    output_schema = definition.get("output_schema") or {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    try:
        validate_schema(arguments, input_schema)
    except ToolExecutionError as exc:
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED, 0,
        )
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "Capability arguments are invalid."
        ) from exc

    if risk_level in {"high", "critical"} and not approved:
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.REJECTED, "approval_required", 0,
        )
        raise ToolApprovalRequired("This capability requires explicit approval before execution.")

    started = time.monotonic()
    try:
        result = adapter(installation, definition, arguments, approved)
    except ToolApprovalRequired:
        raise
    except CapabilityResolutionError:
        latency = int((time.monotonic() - started) * 1000)
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED, latency,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - normalized to a stable failure code
        latency = int((time.monotonic() - started) * 1000)
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED, latency,
        )
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "Capability invocation failed."
        ) from exc

    latency = int((time.monotonic() - started) * 1000)

    # Re-check after the call: a concurrent uninstall must not be reported as success.
    try:
        _ensure_active(installation, in_flight=True)
    except CapabilityResolutionError:
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.FAILED,
            CAPABILITY_UNINSTALLED_IN_FLIGHT, latency,
        )
        raise

    try:
        validate_schema(result, output_schema)
    except ToolExecutionError as exc:
        _record_invocation(
            installation, capability_name, plugin.protocol,
            AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED, latency,
        )
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "Capability result is invalid."
        ) from exc

    _record_invocation(
        installation, capability_name, plugin.protocol,
        AgentCapabilityInvocation.Status.SUCCESS, "", latency,
    )
    return result
