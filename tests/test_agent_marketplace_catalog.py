# ruff: noqa: I001
import pytest
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.urls import include, path
from rest_framework.test import APIClient

# Test-local URLconf: the catalog routes are included by the platform URL
# conf at merge time; this mirrors that include for the route resolution tests.
urlpatterns = [
    path("", include("core.urls")),
    path("", include("core.agent_marketplace_catalog_urls")),
]


def _make_definition(tenant, user, **overrides):
    defaults = {
        "tenant": tenant,
        "created_by": user,
        "plugin_key": "market-agent-a",
        "name": "Market Agent A",
        "description": "Summarizes research reports",
        "version": "1.2.0",
        "protocol": AgentPluginDefinition.Protocol.HTTP_JSON,
        "endpoint_url": "https://market-agent.example.com/v1/tasks",
        "auth_required": True,
        "capabilities": ["summarize", "search"],
        "manifest": {},
        "input_schema": {},
        "output_schema": {},
    }
    defaults.update(overrides)
    return AgentPluginDefinition.objects.create(**defaults)


def _install(tenant, project, user, plugin, status=None):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=status or AgentPluginInstallation.Status.INSTALLED,
        granted_capabilities=list(plugin.capabilities),
        installed_by=user,
    )


@pytest.fixture
def catalog_workspace(db):
    user = get_user_model().objects.create_user(
        username="catalog-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Catalog tenant", slug="catalog-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Catalog project",
        slug="catalog-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    other_project = Project.objects.create(
        tenant=tenant,
        name="Other project",
        slug="other-project",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, other_project, client


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_lists_registered_plugins_without_project_scope(catalog_workspace):
    _, tenant, _, _, client = catalog_workspace
    _make_definition(tenant, catalog_workspace[0])

    response = client.get(f"/api/v1/tenants/{tenant.id}/marketplace/catalog/")

    assert response.status_code == 200
    assert response.data["count"] == 1
    entry = response.data["entries"][0]
    assert entry["plugin_key"] == "market-agent-a"
    assert entry["name"] == "Market Agent A"
    assert entry["description"] == "Summarizes research reports"
    assert entry["version"] == "1.2.0"
    assert entry["protocol"] == "http_json"
    assert entry["enabled"] is True
    assert entry["auth_required"] is True
    assert entry["capabilities"] == [
        {"name": "summarize", "description": "", "risk_level": "low"},
        {"name": "search", "description": "", "risk_level": "low"},
    ]
    assert entry["installed"] is False
    assert entry["installation_status"] == "not_installed"
    assert entry["installation_id"] is None
    assert entry["project_id"] is None


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_marks_current_project_installation(catalog_workspace):
    user, tenant, project, _, client = catalog_workspace
    plugin = _make_definition(tenant, user)
    installation = _install(tenant, project, user, plugin)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id)},
    )

    assert response.status_code == 200
    entry = response.data["entries"][0]
    assert entry["installed"] is True
    assert entry["installation_status"] == "installed"
    assert entry["installation_id"] == str(installation.id)
    assert entry["project_id"] == str(project.id)


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_ignores_installation_in_other_project(catalog_workspace):
    user, tenant, project, other_project, client = catalog_workspace
    plugin = _make_definition(tenant, user)
    _install(tenant, other_project, user, plugin)

    scoped = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id)},
    )
    assert scoped.status_code == 200
    entry = scoped.data["entries"][0]
    assert entry["installed"] is False
    assert entry["installation_status"] == "not_installed"
    assert entry["installation_id"] is None

    other_scoped = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(other_project.id)},
    )
    other_entry = other_scoped.data["entries"][0]
    assert other_entry["installed"] is True
    assert other_entry["installation_status"] == "installed"


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_filters_by_protocol(catalog_workspace):
    user, tenant, _, _, client = catalog_workspace
    _make_definition(tenant, user, plugin_key="http-agent", name="HTTP Agent")
    _make_definition(
        tenant,
        user,
        plugin_key="mcp-agent",
        name="MCP Agent",
        protocol=AgentPluginDefinition.Protocol.MCP,
    )

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"protocol": "mcp"},
    )

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["entries"][0]["plugin_key"] == "mcp-agent"


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_filters_capability_case_insensitive_substring(catalog_workspace):
    user, tenant, _, _, client = catalog_workspace
    _make_definition(tenant, user, plugin_key="summary-agent", name="Summary Agent")
    _make_definition(
        tenant,
        user,
        plugin_key="rewrite-agent",
        name="Rewrite Agent",
        description="Translates drafts into polished prose",
        capabilities=["rewrite"],
    )

    by_capability = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"capability": "SUMmar"},
    )
    assert by_capability.status_code == 200
    assert [e["plugin_key"] for e in by_capability.data["entries"]] == ["summary-agent"]

    by_description = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"capability": "translat"},
    )
    assert [e["plugin_key"] for e in by_description.data["entries"]] == ["rewrite-agent"]


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_filters_installed_flag(catalog_workspace):
    user, tenant, project, _, client = catalog_workspace
    installed_plugin = _make_definition(tenant, user, plugin_key="installed-agent")
    _make_definition(tenant, user, plugin_key="fresh-agent")
    _install(tenant, project, user, installed_plugin)

    installed = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id), "installed": "true"},
    )
    assert installed.status_code == 200
    assert [e["plugin_key"] for e in installed.data["entries"]] == ["installed-agent"]

    uninstalled = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id), "installed": "false"},
    )
    assert [e["plugin_key"] for e in uninstalled.data["entries"]] == ["fresh-agent"]


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_rejects_unknown_protocol_filter(catalog_workspace):
    _, tenant, _, _, client = catalog_workspace

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"protocol": "ftp"},
    )

    assert response.status_code == 400


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_rejects_invalid_installed_filter(catalog_workspace):
    _, tenant, _, _, client = catalog_workspace

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"installed": "maybe"},
    )

    assert response.status_code == 400


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_requires_tenant_membership(catalog_workspace):
    _, tenant, _, _, _ = catalog_workspace
    outsider = get_user_model().objects.create_user(
        username="catalog-outsider",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.get(f"/api/v1/tenants/{tenant.id}/marketplace/catalog/")

    assert response.status_code == 404


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_rejects_project_the_user_cannot_access(catalog_workspace):
    user, tenant, project, _, client = catalog_workspace
    _make_definition(tenant, user)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id)},
    )
    assert response.status_code == 200

    outsider = get_user_model().objects.create_user(
        username="catalog-member",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=outsider,
        role=TenantMembership.Role.MEMBER,
    )
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    denied = outsider_client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/",
        {"project_id": str(project.id)},
    )
    assert denied.status_code == 404

    public = outsider_client.get(f"/api/v1/tenants/{tenant.id}/marketplace/catalog/")
    assert public.status_code == 200


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_aggregates_versions_to_highest(catalog_workspace):
    user, tenant, _, _, client = catalog_workspace
    _make_definition(tenant, user, plugin_key="agent-a", version="1.9.0")
    _make_definition(tenant, user, plugin_key="agent-a", version="1.10.0")
    _make_definition(tenant, user, plugin_key="agent-b", version="2.0.0")
    _make_definition(tenant, user, plugin_key="agent-b", version="1.9.9")

    response = client.get(f"/api/v1/tenants/{tenant.id}/marketplace/catalog/")

    assert response.status_code == 200
    entries = {e["plugin_key"]: e["version"] for e in response.data["entries"]}
    assert entries == {"agent-a": "1.10.0", "agent-b": "2.0.0"}


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_detail_returns_capability_detail_and_installation(catalog_workspace):
    user, tenant, project, _, client = catalog_workspace
    plugin = _make_definition(
        tenant,
        user,
        manifest={
            "capabilities": [
                {
                    "name": "summarize",
                    "description": "Condense a report",
                    "risk_level": "medium",
                    "timeout_seconds": 20,
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "required": ["summary"],
                        "additionalProperties": False,
                    },
                }
            ]
        },
    )
    installation = _install(tenant, project, user, plugin)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/marketplace/catalog/{plugin.plugin_key}/",
        {"project_id": str(project.id)},
    )

    assert response.status_code == 200
    detail = response.data
    assert detail["plugin_key"] == "market-agent-a"
    assert detail["installation_id"] == str(installation.id)
    assert detail["installation_status"] == "installed"
    assert detail["capabilities"] == [
        {
            "name": "summarize",
            "description": "Condense a report",
            "risk_level": "medium",
            "timeout_seconds": 20,
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        }
    ]
    assert detail["manifest"] == plugin.manifest


@pytest.mark.urls("tests.test_agent_marketplace_catalog")
@pytest.mark.django_db
def test_catalog_detail_unknown_key_returns_404(catalog_workspace):
    _, tenant, _, _, client = catalog_workspace

    response = client.get(f"/api/v1/tenants/{tenant.id}/marketplace/catalog/no-such-agent/")

    assert response.status_code == 404
