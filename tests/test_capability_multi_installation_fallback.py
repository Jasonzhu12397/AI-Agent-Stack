"""Resolution must survive one of several installations of a capability going away."""

import pytest
from core.agent_capability_router import (
    CAPABILITY_NOT_INSTALLED,
    CapabilityResolutionError,
    resolve_capability,
)
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


def _manifest():
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {CAPABILITY: {"method": "POST", "path": "/v1/" + CAPABILITY}},
        },
        "capabilities": [
            {
                "name": CAPABILITY,
                "description": "Summarize input text",
                "input_schema": TEXT_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(
        username="fallback-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Fallback tenant", slug="fallback-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Fallback project", slug="fallback-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    return user, tenant, project


def _plugin(tenant, user, *, key, version="1.0.0", enabled=True):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key,
        name="Market agent " + key,
        version=version,
        protocol="http_json",
        endpoint_url="https://market-agent.example.com/v1/invoke",
        auth_required=False,
        capabilities=[CAPABILITY],
        manifest=_manifest(),
        enabled=enabled,
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


def _uninstall(installation):
    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        status="uninstalled"
    )


@pytest.mark.django_db
def test_resolution_falls_back_to_the_remaining_installation(workspace):
    user, tenant, project = workspace
    _install(tenant, project, _plugin(tenant, user, key="agent-primary"), user)
    _install(tenant, project, _plugin(tenant, user, key="agent-backup"), user)

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    previous = resolved.installation.pk

    _uninstall(resolved.installation)

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert resolved.installation.pk != previous
    assert AgentPluginInstallation.objects.get(pk=resolved.installation.pk).status == (
        "installed"
    )


@pytest.mark.django_db
def test_capability_is_not_installed_only_when_every_installation_is_gone(workspace):
    user, tenant, project = workspace
    first = _install(tenant, project, _plugin(tenant, user, key="agent-first"), user)
    second = _install(tenant, project, _plugin(tenant, user, key="agent-second"), user)

    _uninstall(first)
    resolve_capability(str(tenant.id), str(project.id), CAPABILITY)

    _uninstall(second)
    with pytest.raises(CapabilityResolutionError) as error:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert error.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
def test_disabled_definition_does_not_mask_a_usable_installation(workspace):
    user, tenant, project = workspace
    _install(tenant, project, _plugin(tenant, user, key="agent-disabled"), user)
    _install(tenant, project, _plugin(tenant, user, key="agent-enabled"), user)
    AgentPluginDefinition.objects.filter(plugin_key="agent-disabled").update(
        enabled=False
    )

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert resolved.plugin.plugin_key == "agent-enabled"


@pytest.mark.django_db
def test_higher_version_wins_regardless_of_installation_order(workspace):
    user, tenant, project = workspace
    _install(
        tenant, project, _plugin(tenant, user, key="agent-new", version="2.0.0"), user
    )
    _install(
        tenant, project, _plugin(tenant, user, key="agent-old", version="1.0.0"), user
    )

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY)
    assert resolved.plugin.version == "2.0.0"
