import copy

import pytest
from core import plugin_studio
from core.agent_plugin_views import _studio_fingerprint
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.plugin_credentials import resolve
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

OPENAPI_DOCUMENT = {
    "openapi": "3.0.3",
    "info": {"title": "Pet API", "version": "1"},
    "paths": {
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "summary": "Read one pet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {"operationId": "deletePet", "responses": {"204": {"description": "gone"}}},
        },
        "/pets": {
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "x-agent-read-only": True,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}},
            },
            "put": {"operationId": "replacePets", "responses": {"200": {"description": "ok"}}},
        },
    },
}


@pytest.fixture
def studio_workspace(db):
    user = get_user_model().objects.create_user(username="studio-owner", password="pw")
    tenant = Tenant.objects.create(name="Studio tenant", slug="studio-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Studio project", slug="studio-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _discovered_contract(name="get_pets"):
    manifest = {
        "version": 1,
        "capabilities": [
            {
                "name": name,
                "description": "Read pets",
                "input_schema": {
                    "type": "object",
                    "properties": {"petId": {"type": "string"}},
                    "required": ["petId"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "risk_level": "low",
                "timeout_seconds": 30,
                "source": "declared",
            }
        ],
        "http": {
            "base_url": "https://pets.example.com",
            "operations": {
                name: {
                    "method": "GET",
                    "path": "/pets/{petId}",
                    "path_params": {"petid": "petid"},
                    "query_params": {},
                    "body_param": None,
                    "read_only": True,
                }
            },
        },
        "permission_policy": {"default": "read_only", "approval_required": []},
        "studio": {"plugin_type": "openapi", "discovered_at": "now"},
    }
    return {
        "protocol": "http_json",
        "endpoint_url": "https://pets.example.com",
        "manifest": manifest,
        "capabilities": manifest["capabilities"],
        "skipped": [],
        "connection": {
            "status": "success",
            "http_status": 200,
            "authenticated": True,
            "message": "fetched",
        },
        "auth_mode": "bearer",
        "credential_kind": "bearer",
        "credential_configured": True,
        "contract": {"type": "bearer", "token": "secret-token"},
        "generated": {},
    }


def test_openapi_discovery_generates_closed_schemas_and_policy(monkeypatch):
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": value)
    monkeypatch.setattr(
        plugin_studio,
        "_fetch_document",
        lambda url, headers: (copy.deepcopy(OPENAPI_DOCUMENT), 200),
    )
    result = plugin_studio.discover_openapi(
        base_url="https://pets.example.com",
        document_url="https://pets.example.com/openapi.json",
        headers={"Authorization": "Bearer secret-token"},
    )
    names = [entry["name"] for entry in result["capabilities"]]
    assert names == ["getpet", "createpet"]
    assert result["manifest"]["http"]["operations"]["getpet"]["path_params"] == {"petid": "petid"}
    assert result["manifest"]["http"]["operations"]["createpet"]["read_only"] is True
    assert any(item["method"] == "DELETE" for item in result["skipped"])
    get_pet = next(entry for entry in result["capabilities"] if entry["name"] == "getpet")
    assert get_pet["input_schema"]["additionalProperties"] is False
    assert set(get_pet["input_schema"]["properties"]) == {"petid", "verbose"}
    assert result["connection"]["authenticated"] is True


def test_public_url_rejects_private_and_ip_targets():
    with pytest.raises(plugin_studio.PluginStudioError) as ip_error:
        plugin_studio.validate_public_url("https://10.0.0.1/api", "base_url")
    assert ip_error.value.code == "PLUGIN_HOST_REJECTED"
    with pytest.raises(plugin_studio.PluginStudioError) as local_error:
        plugin_studio.validate_public_url("https://metadata.internal/api", "base_url")
    assert local_error.value.code in {"PLUGIN_HOST_REJECTED", "PLUGIN_URL_INVALID"}


def test_publish_seals_credential_and_auto_installs(studio_workspace, monkeypatch):
    _user, tenant, project, client = studio_workspace
    discovered = _discovered_contract()
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda payload: discovered)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/",
        {
            "plugin_type": "openapi",
            "base_url": "https://pets.example.com",
            "auth_type": "bearer",
            "credential": "secret-token",
            "plugin_key": "pet-tools",
            "name": "Pet tools",
            "project_id": str(project.id),
            "selected_capabilities": ["get_pets"],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["changed"] is True
    definition = AgentPluginDefinition.objects.get(tenant=tenant, plugin_key="pet-tools")
    assert definition.version == "1.0.0"
    assert definition.credential_ref.startswith("plugin://")
    assert "secret-token" not in definition.credential_ref
    installation = AgentPluginInstallation.objects.get(project=project, plugin=definition)
    assert installation.credential_ref == definition.credential_ref
    assert installation.granted_capabilities == ["get_pets"]
    assert resolve(definition.credential_ref) == {"type": "bearer", "token": "secret-token"}

    changed = _discovered_contract(name="list_pets")
    changed["capabilities"][0]["name"] = "list_pets"
    changed["manifest"]["capabilities"][0]["name"] = "list_pets"
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda payload: changed)
    second = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/",
        {
            "plugin_type": "openapi",
            "base_url": "https://pets.example.com",
            "auth_type": "bearer",
            "credential": "secret-token",
            "plugin_key": "pet-tools",
            "name": "Pet tools",
            "project_id": str(project.id),
            "selected_capabilities": ["list_pets"],
        },
        format="json",
    )
    assert second.status_code == 201, second.data
    assert second.data["version"] == "1.0.1"
    assert second.data["changed"] is True
    assert _studio_fingerprint(
        AgentPluginDefinition.objects.get(tenant=tenant, version="1.0.1").manifest
    ) == second.data["schema_fingerprint"]


def test_discover_endpoint_rejects_non_admin(studio_workspace, monkeypatch):
    _user, tenant, project, client = studio_workspace
    member = get_user_model().objects.create_user(username="plain-member", password="pw")
    TenantMembership.objects.create(tenant=tenant, user=member, role=TenantMembership.Role.MEMBER)
    other = APIClient()
    other.force_authenticate(user=member)
    response = other.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/discover/",
        {"plugin_type": "openapi", "base_url": "https://pets.example.com"},
        format="json",
    )
    assert response.status_code == 403
