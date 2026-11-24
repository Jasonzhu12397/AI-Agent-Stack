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
from rest_framework.test import APIClient


@pytest.fixture
def agent_plugin_workspace(db):
    user = get_user_model().objects.create_user(
        username="agent-plugin-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Agent plugin tenant", slug="agent-plugin-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Agent plugin project",
        slug="agent-plugin-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _agent_plugin_payload(**overrides):
    payload = {
        "plugin_key": "research-agent",
        "name": "Research Agent",
        "description": "External research agent",
        "version": "1.0.0",
        "protocol": "http_json",
        "endpoint_url": "https://agent.example.com/v1/tasks",
        "auth_required": True,
        "capabilities": ["research.search", "research.summarize"],
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
        "timeout_seconds": 45,
        "max_concurrency": 3,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_tenant_admin_can_register_external_agent_plugin(agent_plugin_workspace):
    _, tenant, _, client = agent_plugin_workspace

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(),
        format="json",
    )

    assert response.status_code == 201
    assert response.data["plugin_key"] == "research-agent"
    assert response.data["protocol"] == AgentPluginDefinition.Protocol.HTTP_JSON
    assert response.data["capabilities"] == ["research.search", "research.summarize"]
    assert response.data["auth_required"] is True


@pytest.mark.django_db
def test_agent_plugin_duplicate_version_returns_conflict(agent_plugin_workspace):
    _, tenant, _, client = agent_plugin_workspace
    url = f"/api/v1/tenants/{tenant.id}/agent-plugins/"
    first = client.post(url, _agent_plugin_payload(), format="json")
    duplicate = client.post(url, _agent_plugin_payload(), format="json")

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert AgentPluginDefinition.objects.filter(
        tenant=tenant,
        plugin_key="research-agent",
        version="1.0.0",
    ).count() == 1


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://agent.example.com/tasks",
        "https://127.0.0.1/tasks",
        "https://localhost/tasks",
        "https://metadata.google.internal/latest",
        "https://agent.example.com:8443/tasks",
    ],
)
@pytest.mark.django_db
def test_agent_plugin_registration_rejects_unsafe_endpoint(agent_plugin_workspace, endpoint_url):
    _, tenant, _, client = agent_plugin_workspace
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(endpoint_url=endpoint_url),
        format="json",
    )
    assert response.status_code == 400
    assert not AgentPluginDefinition.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_agent_plugin_registration_rejects_secret_material_in_schema(agent_plugin_workspace):
    _, tenant, _, client = agent_plugin_workspace
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(
            input_schema={
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
            }
        ),
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_project_installation_requires_explicit_capability_grants_and_secret_reference(
    agent_plugin_workspace,
):
    _, tenant, project, client = agent_plugin_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(),
        format="json",
    )
    assert registered.status_code == 201

    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    missing_credential = client.post(
        install_url,
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["research.search"],
        },
        format="json",
    )
    assert missing_credential.status_code == 400

    undeclared_capability = client.post(
        install_url,
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["research.search", "admin.delete"],
            "credential_ref": "vault://tenant/research-agent",
        },
        format="json",
    )
    assert undeclared_capability.status_code == 400

    installed = client.post(
        install_url,
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["research.search"],
            "credential_ref": "vault://tenant/research-agent",
        },
        format="json",
    )
    assert installed.status_code == 201
    assert installed.data["credential_configured"] is True
    assert installed.data["granted_capabilities"] == ["research.search"]
    assert "vault://tenant/research-agent" not in str(installed.data)
    assert AgentPluginInstallation.objects.filter(
        tenant=tenant,
        project=project,
        plugin_id=registered.data["id"],
    ).count() == 1


@pytest.mark.django_db
def test_agent_plugin_installation_can_be_disabled_and_reenabled(agent_plugin_workspace):
    _, tenant, project, client = agent_plugin_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(auth_required=False),
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["research.search"],
        },
        format="json",
    )
    assert installed.status_code == 201

    status_url = f"/api/v1/agent-plugin-installations/{installed.data['id']}/status/"
    disabled = client.post(status_url, {"status": "disabled"}, format="json")
    assert disabled.status_code == 200
    assert disabled.data["status"] == AgentPluginInstallation.Status.DISABLED

    reenabled = client.post(status_url, {"status": "installed"}, format="json")
    assert reenabled.status_code == 200
    assert reenabled.data["status"] == AgentPluginInstallation.Status.INSTALLED


@pytest.mark.django_db
def test_project_member_cannot_register_tenant_agent_plugin(agent_plugin_workspace):
    _, tenant, _, _ = agent_plugin_workspace
    member = get_user_model().objects.create_user(
        username="agent-plugin-member",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=member,
        role=TenantMembership.Role.MEMBER,
    )
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        _agent_plugin_payload(),
        format="json",
    )

    assert response.status_code == 403
    assert not AgentPluginDefinition.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_definition_payload_flattens_catalog_capability_dicts():
    """Catalog rows store full declarations; the API keeps names plus details."""

    user = get_user_model().objects.create_user(username="catalog-owner", password="x")
    tenant = Tenant.objects.create(name="Catalog tenant", slug="catalog-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    definition = AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="weather",
        name="Weather",
        description="Built-in weather",
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
        endpoint_url="",
        auth_required=False,
        capabilities=[
            {"name": "weather.current", "description": "Current conditions"},
            {"name": "weather.forecast", "description": "Short forecast"},
        ],
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f"/api/v1/tenants/{tenant.id}/agent-plugins/")

    assert response.status_code == 200
    entry = next(item for item in response.data if item["id"] == str(definition.id))
    assert entry["capabilities"] == ["weather.current", "weather.forecast"]
    assert entry["capability_definitions"][0]["name"] == "weather.current"
    assert entry["manifest"]["capabilities"][0]["name"] == "weather.current"


def _install_open_data_plugin(tenant, project, client, user, granted=("weather.current",)):
    definition = AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="weather-agent",
        name="Weather Agent",
        description="Built-in weather",
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
        endpoint_url="",
        auth_required=False,
        capabilities=[
            {
                "name": "weather.current",
                "description": "Current conditions",
                "risk_level": "low",
                "input_schema": {
                    "type": "object",
                    "properties": {"place": {"type": "string"}},
                    "required": ["place"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "place": {"type": "string"},
                        "temperature_c": {"type": "number"},
                    },
                },
            }
        ],
        enabled=True,
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/",
        {"plugin_id": str(definition.id), "granted_capabilities": list(granted)},
        format="json",
    )
    assert installed.status_code == 201, installed.data
    return str(definition.id)


@pytest.mark.django_db
def test_console_can_run_an_installed_capability(agent_plugin_workspace, monkeypatch):
    from core import agent_capability_router as router
    from core.models import AgentCapabilityInvocation

    user, tenant, project, client = agent_plugin_workspace
    plugin_id = _install_open_data_plugin(tenant, project, client, user)
    monkeypatch.setitem(
        router._PROTOCOL_ADAPTERS,
        "open_data",
        lambda installation, definition, arguments, approved: {
            "place": arguments["place"],
            "temperature_c": 5,
        },
    )

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugins/"
        f"{plugin_id}/capabilities/weather.current/invoke/",
        {"arguments": {"place": "西安"}},
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["status"] == "success"
    assert response.data["result"]["temperature_c"] == 5
    invocation = AgentCapabilityInvocation.objects.get(
        installation__project=project, capability="weather.current"
    )
    assert invocation.status == AgentCapabilityInvocation.Status.SUCCESS


@pytest.mark.django_db
def test_console_capability_run_reports_installation_and_argument_errors(agent_plugin_workspace):
    user, tenant, project, client = agent_plugin_workspace
    plugin_id = _install_open_data_plugin(tenant, project, client, user)
    url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugins/"
        f"{plugin_id}/capabilities/weather.current/invoke/"
    )

    invalid = client.post(url, {"arguments": {}}, format="json")
    assert invalid.status_code == 400
    assert invalid.data["error"]["code"] == "CAPABILITY_INVOCATION_FAILED"

    ungranted = client.post(
        url.replace("weather.current", "map.route.plan"),
        {"arguments": {}},
        format="json",
    )
    assert ungranted.status_code == 409
    assert ungranted.data["error"]["code"] == "CAPABILITY_NOT_DECLARED"

    other_project = Project.objects.create(
        tenant=tenant, name="Other project", slug="agent-plugin-other"
    )
    ProjectMembership.objects.create(
        project=other_project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    not_installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{other_project.id}/agent-plugins/"
        f"{plugin_id}/capabilities/weather.current/invoke/",
        {"arguments": {"place": "西安"}},
        format="json",
    )
    assert not_installed.status_code == 409
    assert not_installed.data["error"]["code"] == "CAPABILITY_NOT_INSTALLED"
