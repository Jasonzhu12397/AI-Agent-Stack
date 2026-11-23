"""A live installation must be able to rotate its credential reference.

Installing an auth-required agent plugin stores a credential reference; until
now the only way to point it at a new secret was uninstall and reinstall, which
drops the binding and forces the operator to rebuild workflow nodes. A token
that expires should cost a rotation, not a reinstall.

Rotation is a first-class mutation, so it carries the same guarantees the rest
of the installation lifecycle already has:

* the reference is validated (syntax, registered scheme) and then actually
  resolved, so a rotation that cannot work is refused instead of being stored
  and failing later at invocation time;
* the installation revision is bumped, which is what makes other worker
  processes reject their cached resolution instead of serving the old secret;
* the process-local capability cache is dropped for this installation;
* the audit record names the schemes, never the reference or a value;
* a plugin that does not take a credential, and an uninstalled installation,
  are refused rather than quietly updated.
"""

import pytest
from core.agent_capability_cache import lookup as cache_lookup
from core.agent_capability_router import resolve_capability
from core.models import (
    AgentPluginInstallation,
    AuditEvent,
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
            "base_url": "https://rotate-agent.example.com",
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
def rotation_workspace(db):
    user = get_user_model().objects.create_user(username="rotate-owner")
    tenant = Tenant.objects.create(name="Rotate tenant", slug="rotate-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Rotate project", slug="rotate-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _register_plugin(client, tenant, *, auth_required=True, plugin_key="rotate-agent"):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": plugin_key,
            "name": "Rotate Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://rotate-agent.example.com/v1/invoke",
            "auth_required": auth_required,
            "manifest": _manifest(),
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.data


def _install(client, tenant, project, plugin_id, credential_ref="env://PLUGIN_SECRET_ROTATE_AGENT"):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/",
        {
            "plugin_id": plugin_id,
            "granted_capabilities": ["summarize"],
            "credential_ref": credential_ref,
        },
        format="json",
    )
    assert response.status_code in {200, 201}, response.data
    return response.data


def _rotate_url(installation_id):
    return f"/api/v1/agent-plugin-installations/{installation_id}/credential/"


@pytest.mark.django_db
def test_rotation_bumps_revision_and_drops_the_cached_resolution(
    rotation_workspace, monkeypatch
):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token-before")
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT_V2", "token-after")
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])
    installation = AgentPluginInstallation.objects.get(pk=installed["id"])
    revision_before = installation.revision

    resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert cache_lookup(tenant.id, project.id, "summarize") is not None

    response = client.post(
        _rotate_url(installation.id),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT_V2"},
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["credential_configured"] is True
    installation.refresh_from_db()
    assert installation.credential_ref == "env://PLUGIN_SECRET_ROTATE_AGENT_V2"
    assert installation.revision == revision_before + 1
    assert cache_lookup(tenant.id, project.id, "summarize") is None
    # Resolution after rotation still succeeds; the new secret is what the
    # router would hand to the adapter.
    resolve_capability(str(tenant.id), str(project.id), "summarize")
    cached = cache_lookup(tenant.id, project.id, "summarize")
    assert cached["installation_revision"] == installation.revision


@pytest.mark.django_db
@pytest.mark.parametrize("reference", ["hunter2", "sk-live-abc123", "Bearer abc123"])
def test_rotation_rejects_a_plaintext_value(rotation_workspace, monkeypatch, reference):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token-before")
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    response = client.post(
        _rotate_url(installed["id"]), {"credential_ref": reference}, format="json"
    )

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_REFERENCE_INVALID"
    assert reference not in str(response.data)
    installation = AgentPluginInstallation.objects.get(pk=installed["id"])
    assert installation.credential_ref == "env://PLUGIN_SECRET_ROTATE_AGENT"


@pytest.mark.django_db
def test_rotation_rejects_an_unregistered_scheme(rotation_workspace):
    _, tenant, project, client = rotation_workspace
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    response = client.post(
        _rotate_url(installed["id"]), {"credential_ref": "kv://secret/rotate"}, format="json"
    )

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_PROVIDER_UNSUPPORTED"


@pytest.mark.django_db
def test_rotation_rejects_a_reference_that_does_not_resolve(rotation_workspace, monkeypatch):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token-before")
    monkeypatch.delenv("PLUGIN_SECRET_ROTATE_AGENT_V2", raising=False)
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    response = client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT_V2"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_NOT_FOUND"
    installation = AgentPluginInstallation.objects.get(pk=installed["id"])
    assert installation.credential_ref == "env://PLUGIN_SECRET_ROTATE_AGENT"


@pytest.mark.django_db
def test_rotation_is_audited_with_schemes_only(rotation_workspace, monkeypatch):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token-before")
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT_V2", "token-after")
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT_V2"},
        format="json",
    )

    event = AuditEvent.objects.get(event_type="agent_plugin.credential_rotated")
    rendered = str(event.metadata)
    assert "env" in rendered
    assert "PLUGIN_SECRET_ROTATE_AGENT_V2" not in rendered
    assert "token-after" not in rendered
    assert event.metadata["plugin_key"] == "rotate-agent"


@pytest.mark.django_db
def test_rotation_is_refused_for_a_plugin_without_authentication(rotation_workspace, monkeypatch):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token")
    registered = _register_plugin(client, tenant, auth_required=False, plugin_key="open-agent")
    installed = _install(client, tenant, project, registered["id"], credential_ref="")

    response = client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_NOT_ACCEPTED"
    assert "credential" in response.data["detail"].lower()


@pytest.mark.django_db
def test_rotation_is_refused_for_an_uninstalled_installation(rotation_workspace, monkeypatch):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token")
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    client.post(
        f"/api/v1/agent-plugin-installations/{installed['id']}/status/",
        {"status": "uninstalled"},
        format="json",
    )
    response = client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT"},
        format="json",
    )

    assert response.status_code == 409
    assert response.data["code"] == "INSTALLATION_NOT_ACTIVE"


@pytest.mark.django_db
def test_rotation_requires_project_editor(rotation_workspace, monkeypatch):
    _, tenant, project, client = rotation_workspace
    monkeypatch.setenv("PLUGIN_SECRET_ROTATE_AGENT", "token")
    viewer = get_user_model().objects.create_user(username="rotate-viewer")
    TenantMembership.objects.create(
        tenant=tenant, user=viewer, role=TenantMembership.Role.MEMBER
    )
    ProjectMembership.objects.create(
        project=project, user=viewer, role=ProjectMembership.Role.VIEWER
    )
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    viewer_client = APIClient()
    viewer_client.force_authenticate(user=viewer)
    response = viewer_client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "env://PLUGIN_SECRET_ROTATE_AGENT"},
        format="json",
    )

    assert response.status_code == 403
    installation = AgentPluginInstallation.objects.get(pk=installed["id"])
    assert installation.credential_ref == "env://PLUGIN_SECRET_ROTATE_AGENT"


@pytest.mark.django_db
def test_vault_resolution_failure_is_reported_without_leaking_the_path(rotation_workspace):
    _, tenant, project, client = rotation_workspace
    registered = _register_plugin(client, tenant)
    installed = _install(client, tenant, project, registered["id"])

    response = client.post(
        _rotate_url(installed["id"]),
        {"credential_ref": "vault://merchant/rotate-agent#token"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_RESOLVER_NOT_CONFIGURED"
    assert "merchant/rotate-agent" not in str(response.data)
