"""Every protocol must expose the same resolution semantics.

A market agent speaks one of four protocols. Whichever it speaks, the platform
must answer the same question the same way: is this capability usable, and if
not, why not. This file pins that parity so a protocol cannot quietly grow its
own rules.
"""

import pytest
from core.agent_capability_router import (
    CAPABILITY_DISABLED,
    CAPABILITY_NOT_DECLARED,
    CAPABILITY_NOT_INSTALLED,
    CapabilityResolutionError,
    resolve_capability,
)
from core.agent_plugin_manifest import endpoint_fingerprint
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model

CAPABILITY = "summarize"
ENDPOINT = "https://market-agent.example.com/v1/invoke"
TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
PROTOCOLS = ["http_json", "mcp", "a2a", "openai_compatible"]


def _capability_entry():
    return {
        "name": CAPABILITY,
        "description": "Summarize input text",
        "input_schema": TEXT_INPUT,
        "output_schema": SUMMARY_OUTPUT,
        "risk_level": "low",
        "timeout_seconds": 5,
    }


def _manifest(protocol):
    """A manifest that satisfies the registration rules of each protocol."""
    if protocol in {"mcp", "a2a"}:
        block = {
            "timeout_seconds": 5,
            "discovery": {
                "endpoint": endpoint_fingerprint(ENDPOINT),
                "tools": [CAPABILITY],
            },
        }
        return {protocol: block, "capabilities": [_capability_entry()]}
    if protocol == "openai_compatible":
        return {
            "openai": {"model": "market-model", "timeout_seconds": 5},
            "capabilities": [_capability_entry()],
        }
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {CAPABILITY: {"method": "POST", "path": "/v1/" + CAPABILITY}},
        },
        "capabilities": [_capability_entry()],
    }


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(
        username="parity-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Parity tenant", slug="parity-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Parity project", slug="parity-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    return user, tenant, project


def _plugin(tenant, user, protocol, *, key=None, capability=True):
    manifest = _manifest(protocol)
    if not capability:
        manifest["capabilities"] = [
            dict(_capability_entry(), name="invoices.list")
        ]
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key or ("agent-" + protocol.replace("_", "-")),
        name="Market agent " + protocol,
        version="1.0.0",
        protocol=protocol,
        endpoint_url=ENDPOINT,
        auth_required=False,
        capabilities=[CAPABILITY],
        manifest=manifest,
    )


def _install(tenant, project, plugin, user):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status="installed",
        granted_capabilities=[CAPABILITY],
        installed_by=user,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_protocol_resolves_the_same_way(workspace, protocol):
    user, tenant, project = workspace
    plugin = _plugin(tenant, user, protocol)
    installation = _install(tenant, project, plugin, user)

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert resolved.installation.pk == installation.pk
    assert resolved.plugin.protocol == protocol
    assert resolved.definition["name"] == CAPABILITY


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_protocol_reports_not_installed_after_uninstall(workspace, protocol):
    user, tenant, project = workspace
    plugin = _plugin(tenant, user, protocol)
    installation = _install(tenant, project, plugin, user)
    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        status="uninstalled"
    )

    with pytest.raises(CapabilityResolutionError) as error:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert error.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_protocol_reports_disabled_definition_the_same_way(workspace, protocol):
    user, tenant, project = workspace
    plugin = _plugin(tenant, user, protocol)
    _install(tenant, project, plugin, user)
    AgentPluginDefinition.objects.filter(pk=plugin.pk).update(enabled=False)

    with pytest.raises(CapabilityResolutionError) as error:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert error.value.code == CAPABILITY_DISABLED


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_protocol_reports_not_declared_the_same_way(workspace, protocol):
    user, tenant, project = workspace
    plugin = _plugin(tenant, user, protocol, capability=False)
    _install(tenant, project, plugin, user)

    with pytest.raises(CapabilityResolutionError) as error:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert error.value.code == CAPABILITY_NOT_DECLARED
