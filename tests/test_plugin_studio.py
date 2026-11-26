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
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
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
    assert (
        _studio_fingerprint(
            AgentPluginDefinition.objects.get(tenant=tenant, version="1.0.1").manifest
        )
        == second.data["schema_fingerprint"]
    )


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


def test_oauth_refresh_token_contract_keeps_secrets_out_of_manifest(monkeypatch):
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": str(value))
    contract = plugin_studio._auth_from_request(
        {
            "auth_type": "oauth2",
            "credential": "client-secret",
            "auth_config": {
                "grant": "refresh_token",
                "token_url": "https://auth.example.com/oauth/token",
                "client_id": "client-id",
                "scope": "read:tools",
                "refresh_token": "refresh-value",
            },
        }
    )
    assert contract["type"] == "oauth2"
    assert contract["grant"] == "refresh_token"
    assert contract["client_secret"] == "client-secret"
    assert contract["refresh_token"] == "refresh-value"
    assert "client-secret" not in str(contract.get("scope", ""))


def test_version_history_lists_and_activates_earlier_versions(studio_workspace, monkeypatch):
    _user, tenant, project, client = studio_workspace
    first = _discovered_contract(name="get_pets")
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda payload: first)
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
    second = _discovered_contract(name="list_pets")
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda payload: second)
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
            "selected_capabilities": ["list_pets"],
        },
        format="json",
    )
    assert response.status_code == 201, response.data

    history = client.get(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/versions/?plugin_key=pet-tools"
    )
    assert history.status_code == 200, history.data
    assert [entry["version"] for entry in history.data] == ["1.0.1", "1.0.0"]
    assert history.data[0]["is_active"] is True

    old_id = history.data[1]["id"]
    activated = client.post(f"/api/v1/agent-plugins/{old_id}/activate/", {}, format="json")
    assert activated.status_code == 200, activated.data
    definition = AgentPluginDefinition.objects.get(pk=old_id)
    assert definition.enabled is True
    assert AgentPluginDefinition.objects.get(tenant=tenant, version="1.0.1").enabled is False
    assert AgentPluginInstallation.objects.filter(tenant=tenant).count() == 1
    installation = AgentPluginInstallation.objects.get(tenant=tenant)
    assert installation.plugin_id == definition.id
    assert installation.plugin_version == "1.0.0"
    assert installation.status == AgentPluginInstallation.Status.INSTALLED
    assert installation.granted_capabilities == []
    assert activated.data["migration"] == {
        "installations": 1,
        "dropped_capabilities": {str(installation.id): ["list_pets"]},
    }


def test_pasted_openapi_document_generates_capabilities_without_fetch(
    studio_workspace, monkeypatch
):
    _user, tenant, project, client = studio_workspace

    def fail_fetch(*args, **kwargs):
        raise AssertionError("A pasted document must not trigger a document fetch")

    monkeypatch.setattr(plugin_studio, "_fetch_document", fail_fetch)
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": str(value))
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/discover/",
        {
            "plugin_type": "openapi",
            "base_url": "https://pets.example.com",
            "auth_type": "none",
            "document": OPENAPI_DOCUMENT,
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    names = [entry["name"] for entry in response.data["capabilities"]]
    assert "getpet" in names
    assert "deletepet" not in names


def test_credential_rotation_does_not_fork_the_schema_version(studio_workspace, monkeypatch):
    _user, tenant, project, client = studio_workspace
    payload = {
        "plugin_type": "openapi",
        "base_url": "https://pets.example.com",
        "auth_type": "bearer",
        "credential": "old-token",
        "plugin_key": "rotate-tools",
        "name": "Rotate tools",
        "project_id": str(project.id),
        "selected_capabilities": ["get_pets"],
    }
    discovered = _discovered_contract()
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda body: discovered)
    first = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/", payload, format="json"
    )
    assert first.status_code == 201, first.data
    definition = AgentPluginDefinition.objects.get(tenant=tenant, plugin_key="rotate-tools")
    old_ref = definition.credential_ref
    other_project = Project.objects.create(
        tenant=tenant, name="Second project", slug="second-project"
    )
    ProjectMembership.objects.create(
        project=other_project, user=_user, role=ProjectMembership.Role.ADMIN
    )
    other_installation = AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=other_project,
        plugin=definition,
        plugin_version=definition.version,
        granted_capabilities=["get_pets"],
        credential_ref=old_ref,
        installed_by=_user,
    )

    rotated = copy.deepcopy(discovered)
    rotated["contract"] = {"type": "bearer", "token": "new-token"}
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda body: rotated)
    second = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/", payload, format="json"
    )
    assert second.status_code == 200, second.data
    assert second.data["changed"] is False
    assert second.data["version"] == "1.0.0"
    definition.refresh_from_db()
    assert definition.credential_ref != old_ref
    other_installation.refresh_from_db()
    assert other_installation.credential_ref == definition.credential_ref
    assert resolve(definition.credential_ref, tenant_id=tenant.id) == {
        "type": "bearer",
        "token": "new-token",
    }


def test_failed_publish_rolls_back_the_sealed_credential(studio_workspace, monkeypatch):
    from core.models import AgentPluginCredential

    _user, tenant, project, client = studio_workspace
    discovered = _discovered_contract()
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda body: discovered)

    def explode(*args, **kwargs):
        from django.db import IntegrityError

        raise IntegrityError("duplicate")

    monkeypatch.setattr(AgentPluginDefinition.objects, "create", explode)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/",
        {
            "plugin_type": "openapi",
            "base_url": "https://pets.example.com",
            "auth_type": "bearer",
            "credential": "orphan-token",
            "plugin_key": "orphan-tools",
            "name": "Orphan tools",
            "project_id": str(project.id),
            "selected_capabilities": ["get_pets"],
        },
        format="json",
    )
    assert response.status_code == 409
    assert not AgentPluginCredential.objects.filter(tenant=tenant).exists()


def test_mcp_discovery_grants_no_execution_authority(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def initialize(self):
            return None

        def list_tools(self):
            return [
                {
                    "name": "delete_cluster",
                    "description": "Delete a production cluster",
                    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
                }
            ]

    monkeypatch.setattr("core.mcp_connector.McpClient", FakeClient)
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": value)
    result = plugin_studio.discover_mcp(
        endpoint_url="https://mcp.example.com/rpc", contract={"type": "bearer", "token": "t"}
    )
    capability = result["manifest"]["capabilities"][0]
    assert capability["risk_level"] == "unclassified"
    assert capability["review_required"] is True
    assert result["manifest"]["permission_policy"]["default"] == "deny"
    assert result["manifest"]["permission_policy"]["approval_required"] == ["delete_cluster"]
    assert result["manifest"]["permission_policy"]["max_risk"] == "unclassified"


def test_openapi_keeps_path_parameters_variants_and_response_schema(monkeypatch):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Variant API", "version": "1"},
        "paths": {
            "/pets/{petId}": {
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "string"},
                    },
                ],
                "get": {
                    "operationId": "readPet",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                        },
                                        "required": ["id"],
                                    }
                                }
                            },
                        }
                    },
                },
            },
            "/pets": {
                "post": {
                    "operationId": "createPet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "pet": {
                                            "oneOf": [
                                                {
                                                    "type": "object",
                                                    "properties": {"name": {"type": "string"}},
                                                    "required": ["name"],
                                                },
                                                {
                                                    "type": "object",
                                                    "properties": {"tag": {"type": "string"}},
                                                },
                                            ]
                                        }
                                    },
                                    "required": ["pet"],
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            },
        },
    }
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": value)

    result = plugin_studio.discover_openapi(
        base_url="https://pets.example.com",
        document_url="https://pets.example.com/openapi.json",
        headers={},
        document=document,
    )

    read_pet = next(item for item in result["capabilities"] if item["name"] == "readpet")
    assert read_pet["input_schema"]["properties"]["petid"] == {
        "type": "string",
        "maxLength": 65536,
    }
    assert read_pet["input_schema"]["properties"]["limit"]["type"] == "integer"
    assert read_pet["output_schema"]["properties"]["name"]["type"] == "string"
    assert read_pet["output_schema"]["required"] == ["id"]

    create_pet = next(item for item in result["capabilities"] if item["name"] == "createpet")
    variant = create_pet["input_schema"]["properties"]["body"]["properties"]["pet"]
    assert variant["type"] == "object"
    assert set(variant["properties"]) == {"name", "tag"}
    assert variant["required"] == []


def test_openapi_rejects_unbounded_variant_unions(monkeypatch):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Union API", "version": "1"},
        "paths": {
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"value": {"oneOf": [{"type": "string"}] * 9}},
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    monkeypatch.setattr(plugin_studio, "validate_public_url", lambda value, field="url": value)

    with pytest.raises(plugin_studio.PluginStudioError) as exc:
        plugin_studio.discover_openapi(
            base_url="https://pets.example.com",
            document_url="https://pets.example.com/openapi.json",
            headers={},
            document=document,
        )
    assert exc.value.code == "PLUGIN_SCHEMA_UNSUPPORTED"


def test_oauth_authorization_code_flow_persists_a_refreshable_credential(
    studio_workspace, monkeypatch
):
    user, tenant, _project, client = studio_workspace
    monkeypatch.setattr(
        plugin_studio,
        "validate_public_url",
        lambda value, field="url": str(value),
    )
    calls = []

    def fake_token_request(contract, timeout=15.0):
        calls.append(dict(contract))
        if contract["grant"] == "authorization_code":
            assert contract["code"] == "provider-code"
            assert contract["code_verifier"]
            return {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        assert contract["grant"] == "refresh_token"
        assert contract["refresh_token"] == "refresh-1"
        return {"access_token": "access-2", "expires_in": 3600}

    monkeypatch.setattr(plugin_studio, "_oauth_token_request", fake_token_request)

    started = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/authorize/",
        {
            "authorization_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "redirect_uri": "https://console.example.com/plugins/oauth/callback",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scope": "read:tools",
        },
        format="json",
    )
    assert started.status_code == 201, started.data
    url = started.data["authorize_url"]
    assert "response_type=code" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    state = started.data["state"]

    completed = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/callback/",
        {"code": "provider-code", "state": state},
        format="json",
    )
    assert completed.status_code == 201, completed.data
    reference = completed.data["credential"]
    assert reference.startswith("plugin://")
    stored = resolve(reference, tenant_id=tenant.id)
    assert stored == {"type": "bearer", "token": "access-2"}
    assert calls[0]["grant"] == "authorization_code"
    assert calls[1]["grant"] == "refresh_token"

    from core.models import AgentPluginCredential

    credential = AgentPluginCredential.objects.get(pk=reference.split("://", 1)[1])
    import json

    contract = json.loads(
        plugin_studio.open_sealed(credential.sealed_value, plugin_studio.CREDENTIAL_SCOPE)
    )
    assert contract["grant"] == "refresh_token"
    assert contract["refresh_token"] == "refresh-1"
    assert "code" not in contract
    assert "code_verifier" not in contract


def test_oauth_callback_rejects_an_expired_state(studio_workspace, monkeypatch):
    _user, tenant, _project, client = studio_workspace
    monkeypatch.setattr(
        plugin_studio,
        "validate_public_url",
        lambda value, field="url": str(value),
    )
    monkeypatch.setattr(plugin_studio, "OAUTH_STATE_TTL_SECONDS", -1)
    started = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/authorize/",
        {
            "authorization_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "redirect_uri": "https://console.example.com/plugins/oauth/callback",
            "client_id": "client-id",
        },
        format="json",
    )
    assert started.status_code == 201
    completed = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/callback/",
        {"code": "provider-code", "state": started.data["state"]},
        format="json",
    )
    assert completed.status_code == 400
    assert "expired" in completed.data["error"]["message"].lower()


def test_publish_binds_an_authorization_code_credential_reference(studio_workspace, monkeypatch):
    _user, tenant, project, client = studio_workspace
    monkeypatch.setattr(
        plugin_studio, "validate_public_url", lambda value, field="url": str(value)
    )

    def fake_token_request(contract, timeout=15.0):
        if contract["grant"] == "authorization_code":
            return {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
            }
        return {"access_token": "access-2", "expires_in": 3600}

    monkeypatch.setattr(plugin_studio, "_oauth_token_request", fake_token_request)
    started = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/authorize/",
        {
            "authorization_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "redirect_uri": "https://console.example.com/plugins/oauth/callback",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scope": "read:tools",
        },
        format="json",
    )
    completed = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/oauth/callback/",
        {"code": "provider-code", "state": started.data["state"]},
        format="json",
    )
    reference = completed.data["credential"]

    discovered = _discovered_contract(name="get_pets")
    discovered["contract"] = {"type": "reference", "reference": reference}
    monkeypatch.setattr(plugin_studio, "discover_plugin", lambda payload: discovered)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/studio/publish/",
        {
            "plugin_type": "openapi",
            "base_url": "https://pets.example.com",
            "auth_type": "oauth2",
            "auth_config": {"grant": "authorization_code"},
            "credential": reference,
            "plugin_key": "oauth-pet-tools",
            "name": "OAuth pet tools",
            "project_id": str(project.id),
            "selected_capabilities": ["get_pets"],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    from core.models import AgentPluginDefinition

    definition = AgentPluginDefinition.objects.get(
        tenant=tenant, plugin_key="oauth-pet-tools"
    )
    assert definition.credential_ref == reference
    assert resolve(reference, tenant_id=tenant.id) == {"type": "bearer", "token": "access-2"}
