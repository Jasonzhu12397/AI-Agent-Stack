import pytest
from core.models import (
    AgentDefinition,
    AgentPluginDefinition,
    AgentPluginInstallation,
    AgentVersion,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.plugin_agent_binding import (
    BINDING_CAPABILITY_NOT_DECLARED,
    BINDING_INSTALLATION_INACTIVE,
    AgentBindingError,
    bind_capability,
    bind_installation,
    list_marketplace_agents,
)
from django.contrib.auth import get_user_model
from django.urls import include, path
from rest_framework.test import APIClient

# Test-local URLconf: the binding routes are included by the platform URL
# conf at merge time; this mirrors that include for the route resolution tests.
urlpatterns = [
    path("", include("core.urls")),
    path("", include("core.plugin_agent_urls")),
]


SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _manifest(capability="summarize"):
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {
                capability: {
                    "method": "GET",
                    "path": "/v1/" + capability,
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": capability,
                "description": "Summarize input text",
                "input_schema": TEXT_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def binding_workspace(db):
    user = get_user_model().objects.create_user(
        username="binding-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Binding tenant", slug="binding-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Binding project", slug="binding-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, version="1.0.0", key="market-agent-a", manifest=None):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key,
        name="Market Agent",
        version=version,
        protocol="http_json",
        endpoint_url="https://market-agent.example.com/v1/invoke",
        auth_required=False,
        capabilities=["summarize"],
        manifest=manifest or _manifest(),
    )


def _install(tenant, project, plugin, user, granted=None, status="installed"):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=status,
        granted_capabilities=granted or ["summarize"],
        installed_by=user,
    )


@pytest.mark.django_db
def test_bind_capability_is_idempotent(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    first = bind_capability(installation, "summarize", user)
    second = bind_capability(installation, "summarize", user)

    assert first.definition.id == second.definition.id
    assert first.version.id == second.version.id
    assert AgentDefinition.objects.filter(project=project).count() == 1
    assert AgentVersion.objects.filter(agent=first.definition).count() == 1
    snapshot = second.snapshot
    assert snapshot["kind"] == "marketplace_agent"
    assert snapshot["plugin_key"] == "market-agent-a"
    assert snapshot["plugin_version"] == "1.0.0"
    assert snapshot["installation_id"] == str(installation.id)
    assert snapshot["capability"] == "summarize"
    assert snapshot["protocol"] == "http_json"
    assert snapshot["risk_level"] == "low"


@pytest.mark.django_db
def test_bind_adds_new_version_when_plugin_version_changes(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    first = bind_capability(installation, "summarize", user)

    installation.plugin_version = "1.1.0"
    installation.save(update_fields=["plugin_version", "updated_at"])
    second = bind_capability(installation, "summarize", user)

    # Same definition, one extra version; the old snapshot is preserved.
    assert second.definition.id == first.definition.id
    versions = (
        AgentVersion.objects.filter(agent=first.definition)
        .order_by("version")
    )
    assert versions.count() == 2
    assert versions[0].config_snapshot["plugin_version"] == "1.0.0"
    assert versions[1].config_snapshot["plugin_version"] == "1.1.0"
    assert second.version.version == 2
    definition = AgentDefinition.objects.get(pk=first.definition.id)
    assert definition.current_version == 2


@pytest.mark.django_db
def test_bind_rejects_uninstalled_installation(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(
        tenant, project, plugin, user, status="uninstalled"
    )

    with pytest.raises(AgentBindingError) as exc:
        bind_capability(installation, "summarize", user)
    assert exc.value.code == BINDING_INSTALLATION_INACTIVE
    assert not AgentDefinition.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_bind_rejects_disabled_installation(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user, status="disabled")

    with pytest.raises(AgentBindingError) as exc:
        bind_capability(installation, "summarize", user)
    assert exc.value.code == BINDING_INSTALLATION_INACTIVE
    assert not AgentDefinition.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_bind_rejects_undeclared_capability(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    with pytest.raises(AgentBindingError) as exc:
        bind_capability(installation, "not-declared", user)
    assert exc.value.code == BINDING_CAPABILITY_NOT_DECLARED
    assert not AgentDefinition.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_bind_installation_defaults_to_all_declared_capabilities(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    bound = bind_installation(installation, None, user)

    assert [item.snapshot["capability"] for item in bound] == ["summarize"]
    assert AgentDefinition.objects.filter(project=project).count() == 1


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_binding")
def test_bind_agent_api_is_idempotent_and_requires_editor(binding_workspace):
    user, tenant, project, client = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    first = client.post(
        f"/api/v1/agent-plugin-installations/{installation.id}/bind-agent/",
        {"capabilities": ["summarize"]},
        format="json",
    )
    assert first.status_code == 200
    assert first.data["bound"][0]["slug"]
    assert first.data["bound"][0]["agent_version"] == 1

    second = client.post(
        f"/api/v1/agent-plugin-installations/{installation.id}/bind-agent/",
        {"capabilities": ["summarize"]},
        format="json",
    )
    assert second.status_code == 200
    assert second.data["bound"][0]["definition_id"] == first.data["bound"][0]["definition_id"]
    assert second.data["bound"][0]["agent_version_id"] == first.data["bound"][0]["agent_version_id"]
    assert second.data["bound"][0]["version_created"] is False

    viewer = get_user_model().objects.create_user(
        username="binding-viewer", password="test-password"
    )
    TenantMembership.objects.create(
        tenant=tenant, user=viewer, role=TenantMembership.Role.MEMBER
    )
    ProjectMembership.objects.create(
        project=project, user=viewer, role=ProjectMembership.Role.VIEWER
    )
    viewer_client = APIClient()
    viewer_client.force_authenticate(user=viewer)
    forbidden = viewer_client.post(
        f"/api/v1/agent-plugin-installations/{installation.id}/bind-agent/",
        {"capabilities": ["summarize"]},
        format="json",
    )
    assert forbidden.status_code == 403

    outsider = get_user_model().objects.create_user(
        username="binding-outsider", password="test-password"
    )
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)
    missing = outsider_client.post(
        f"/api/v1/agent-plugin-installations/{installation.id}/bind-agent/",
        {"capabilities": ["summarize"]},
        format="json",
    )
    assert missing.status_code == 404


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_binding")
def test_bind_api_rejects_inactive_installation(binding_workspace):
    user, tenant, project, client = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(
        tenant, project, plugin, user, status="uninstalled"
    )

    response = client.post(
        f"/api/v1/agent-plugin-installations/{installation.id}/bind-agent/",
        {"capabilities": ["summarize"]},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["code"] == BINDING_INSTALLATION_INACTIVE
    assert not AgentDefinition.objects.filter(project=project).exists()


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_binding")
def test_marketplace_agent_list_flags_unavailable_installations(binding_workspace):
    user, tenant, project, client = binding_workspace
    active_plugin = _make_plugin(tenant, user, key="active-agent")
    active_installation = _install(tenant, project, active_plugin, user)
    inactive_plugin = _make_plugin(tenant, user, key="disabled-agent")
    inactive_installation = _install(tenant, project, inactive_plugin, user)
    unbound_plugin = _make_plugin(tenant, user, key="unbound-agent")
    _install(tenant, project, unbound_plugin, user)

    bind_capability(active_installation, "summarize", user)
    bind_capability(inactive_installation, "summarize", user)
    inactive_installation.status = AgentPluginInstallation.Status.DISABLED
    inactive_installation.save(update_fields=["status", "updated_at"])

    entries = list_marketplace_agents(tenant, project)
    by_key = {entry["plugin_key"]: entry for entry in entries}
    assert set(by_key) == {"active-agent", "disabled-agent"}

    active = by_key["active-agent"]
    assert active["available"] is True
    assert active["availability_reason"] is None
    assert active["installation_status"] == "installed"
    assert active["agent_version"] == 1

    disabled = by_key["disabled-agent"]
    assert disabled["available"] is False
    assert disabled["availability_reason"] == "installation_disabled"

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/marketplace-agents/"
    )
    assert response.status_code == 200
    assert len(response.data["agents"]) == 2


@pytest.mark.django_db
def test_marketplace_agent_list_marks_uninstalled_and_version_mismatch(binding_workspace):
    user, tenant, project, _ = binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    bound = bind_capability(installation, "summarize", user)

    installation.status = AgentPluginInstallation.Status.UNINSTALLED
    installation.save(update_fields=["status", "updated_at"])
    entries = list_marketplace_agents(tenant, project)
    assert len(entries) == 1
    assert entries[0]["available"] is False
    assert entries[0]["availability_reason"] == "installation_uninstalled"

    installation.status = AgentPluginInstallation.Status.INSTALLED
    installation.plugin_version = "2.0.0"
    installation.save(update_fields=["status", "plugin_version", "updated_at"])
    entries = list_marketplace_agents(tenant, project)
    assert entries[0]["available"] is False
    assert entries[0]["availability_reason"] == "version_mismatch"

    # The pinned old version still exists and keeps its original snapshot.
    old = AgentVersion.objects.get(pk=bound.version.id)
    assert old.config_snapshot["plugin_version"] == "1.0.0"


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_binding")
def test_marketplace_agent_list_rejects_non_project_member(binding_workspace):
    _, tenant, project, client = binding_workspace
    outsider = get_user_model().objects.create_user(
        username="binding-list-outsider", password="test-password"
    )
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    response = outsider_client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/marketplace-agents/"
    )
    assert response.status_code == 404
