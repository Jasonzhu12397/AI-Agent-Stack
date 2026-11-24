# ruff: noqa: I001
"""Credential flow gates for the agent plugin installation endpoint.

These tests pin the programmable error codes returned when an auth-required
plugin is installed without a reference, when the reference shape is invalid,
or when its scheme has no registered credential provider. Successful installs
are then checked against the capability resolver so the accepted reference
cannot remain a dangling record. No real secret backend is contacted: env
variables are patched and the vault HTTP client is replaced with an in-memory
mock transport.
"""

import httpx
import pytest

from core.agent_capability_router import resolve_capability
from core.models import (
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

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


def _manifest():
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {
                "summarize": {
                    "method": "GET",
                    "path": "/v1/summarize",
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": "summarize",
                "description": "Summarize input text",
                "input_schema": TEXT_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def credential_workspace(db):
    user = get_user_model().objects.create_user(
        username="credflow-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Credflow tenant", slug="credflow-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Credflow project", slug="credflow-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _register_plugin(client, tenant, *, auth_required=True, plugin_key="secure-agent"):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": plugin_key,
            "name": "Secure Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": auth_required,
            "manifest": _manifest(),
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.data


def _install_url(tenant, project):
    return f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"


@pytest.mark.django_db
def test_install_auth_required_plugin_without_reference_is_rejected(credential_workspace):
    _, tenant, project, client = credential_workspace
    registered = _register_plugin(client, tenant, auth_required=True)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_id": registered["id"],
            "granted_capabilities": ["summarize"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "CREDENTIAL_REQUIRED"
    assert "credential_ref" in response.data["error"]["message"]
    assert not AgentPluginInstallation.objects.filter(
        tenant=tenant, project=project, plugin_id=registered["id"]
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("reference", ["hunter2", "sk-live-abc123", "Bearer abc123"])
def test_install_rejects_plaintext_or_malformed_reference(credential_workspace, reference):
    _, tenant, project, client = credential_workspace
    registered = _register_plugin(client, tenant, auth_required=True)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_id": registered["id"],
            "granted_capabilities": ["summarize"],
            "credential_ref": reference,
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "CREDENTIAL_REFERENCE_INVALID"
    assert reference not in response.data["error"]["message"]
    assert not AgentPluginInstallation.objects.filter(
        tenant=tenant, project=project, plugin_id=registered["id"]
    ).exists()


@pytest.mark.django_db
def test_install_rejects_reference_with_unregistered_scheme(credential_workspace):
    _, tenant, project, client = credential_workspace
    registered = _register_plugin(client, tenant, auth_required=True)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_id": registered["id"],
            "granted_capabilities": ["summarize"],
            "credential_ref": "kv://secret/capflow-token",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "CREDENTIAL_PROVIDER_UNSUPPORTED"
    assert not AgentPluginInstallation.objects.filter(
        tenant=tenant, project=project, plugin_id=registered["id"]
    ).exists()


@pytest.mark.django_db
def test_install_accepts_env_reference_and_resolves_capability(
    credential_workspace, monkeypatch
):
    _, tenant, project, client = credential_workspace
    monkeypatch.setenv("PLUGIN_SECRET_CAPFLOW_TOKEN", "bearer-token-value")
    registered = _register_plugin(client, tenant, auth_required=True)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_key": registered["plugin_key"],
            "plugin_version": registered["version"],
            "granted_capabilities": ["summarize"],
            "credential_ref": "env://PLUGIN_SECRET_CAPFLOW_TOKEN",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["credential_configured"] is True
    installation = AgentPluginInstallation.objects.get(
        tenant=tenant, project=project, plugin_id=registered["id"]
    )
    assert installation.credential_ref == "env://PLUGIN_SECRET_CAPFLOW_TOKEN"

    listed = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-capabilities/"
    )
    assert listed.status_code == 200
    listed_names = [item["name"] for item in listed.data["capabilities"]]
    assert "summarize" in listed_names

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.installation.id == installation.id
    assert resolved.plugin.plugin_key == registered["plugin_key"]


def _mock_vault_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"data": {"value": "vault-bearer-42"}}},
        )

    return httpx.MockTransport(handler)


@pytest.mark.django_db
def test_install_accepts_vault_reference_with_stubbed_backend(
    credential_workspace, settings, monkeypatch
):
    _, tenant, project, client = credential_workspace
    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    real_client = httpx.Client

    def stub_client_factory(**options):
        options.pop("transport", None)
        return real_client(transport=_mock_vault_transport(), **options)

    monkeypatch.setattr(
        "core.plugin_credentials.httpx.Client", stub_client_factory
    )
    registered = _register_plugin(client, tenant, auth_required=True)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_id": registered["id"],
            "granted_capabilities": ["summarize"],
            "credential_ref": "vault://secret/secure-agent",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["credential_configured"] is True

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.plugin.plugin_key == registered["plugin_key"]


@pytest.mark.django_db
def test_install_open_plugin_succeeds_without_reference(credential_workspace):
    _, tenant, project, client = credential_workspace
    registered = _register_plugin(client, tenant, auth_required=False)

    response = client.post(
        _install_url(tenant, project),
        {
            "plugin_id": registered["id"],
            "granted_capabilities": ["summarize"],
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["credential_configured"] is False

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.plugin.plugin_key == registered["plugin_key"]
