# ruff: noqa: I001
import pytest
from core.models import (
    PluginConnection,
    PluginDefinition,
    PluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.tool_router import ToolExecutionError
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def plugin_workspace(db):
    user = get_user_model().objects.create_user(
        username="plugin-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Plugin tenant", slug="plugin-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Plugin project",
        slug="plugin-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def plugin_payload(tenant):
    return {
        "tenant_id": str(tenant.id),
        "plugin_key": "github-reader",
        "name": "GitHub Reader",
        "description": "Read-only repository metadata connector.",
        "version": "1.0.0",
        "connector_type": "http",
        "maintainer": "Platform Team",
        "license": "Apache-2.0",
        "source_url": "https://example.invalid/github-reader.json",
        "manifest": {"category": "developer-tools"},
        "tools": [
            {
                "name": "list-repositories",
                "description": "List repositories visible to the connection.",
                "input_schema": {
                    "type": "object",
                    "properties": {"owner": {"type": "string"}},
                    "required": ["owner"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"repositories": {"type": "array"}},
                    "additionalProperties": False,
                },
                "required_permissions": ["github:repo:read"],
                "risk_level": "low",
                "timeout_seconds": 20,
                "retry_policy": {"max_attempts": 3, "backoff_seconds": 2},
                "idempotency_mode": "read_only",
            }
        ],
    }


def register_plugin(client, tenant):
    return client.post(
        "/api/v1/connectors/",
        plugin_payload(tenant),
        format="json",
    )


@pytest.mark.django_db
def test_plugin_registry_requires_review_before_project_installation(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    registered = register_plugin(client, tenant)

    assert registered.status_code == 201
    assert registered.data["review_status"] == PluginDefinition.ReviewStatus.DRAFT
    assert registered.data["enabled"] is False
    assert registered.data["tools"][0]["name"] == "list-repositories"

    blocked = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )

    assert blocked.status_code == 404
    assert PluginInstallation.objects.count() == 0


@pytest.mark.django_db
def test_approved_plugin_can_be_installed_with_explicit_permission_and_credential_ref(
    plugin_workspace,
):
    user, tenant, project, client = plugin_workspace
    registered = register_plugin(client, tenant)
    approved = client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {"api_base": "https://api.github.com"},
        },
        format="json",
    )

    assert approved.status_code == 200
    assert approved.data["review_status"] == PluginDefinition.ReviewStatus.APPROVED
    assert installed.status_code == 201
    assert installed.data["plugin_version"] == "1.0.0"
    assert installed.data["granted_permissions"] == ["github:repo:read"]

    connection = client.post(
        f"/api/v1/plugin-installations/{installed.data['id']}/connections/",
        {
            "name": "github-production",
            "auth_type": "credential_ref",
            "credential_ref": "vault://platform/github-reader",
            "config": {"base_url": "https://api.github.com"},
        },
        format="json",
    )

    assert connection.status_code == 201
    assert connection.data["status"] == PluginConnection.Status.PENDING
    assert connection.data["credential_configured"] is True
    assert "credential_ref" not in connection.data
    listed = client.get(
        f"/api/v1/plugin-installations/{installed.data['id']}/connections/"
    )
    assert listed.status_code == 200
    assert listed.data["results"][0]["credential_configured"] is True
    assert "credential_ref" not in listed.data["results"][0]
    assert PluginConnection.objects.count() == 1


@pytest.mark.django_db
def test_plugin_registration_rejects_plaintext_secret_in_manifest(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    payload = plugin_payload(tenant)
    payload["manifest"]["token"] = "must-not-be-stored"

    response = client.post("/api/v1/connectors/", payload, format="json")

    assert response.status_code == 400
    assert PluginDefinition.objects.count() == 0


@pytest.mark.django_db
def test_plugin_connection_rejects_plaintext_credential(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    registered = register_plugin(client, tenant)
    client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )

    invalid_references = [
        "ghp_plaintext_token",
        "token://platform/github-reader",
        "https://vault.example.com/path",
        "https://user:password@example.com/secret",
        "file:///etc/secrets/token",
    ]
    for reference in invalid_references:
        response = client.post(
            f"/api/v1/plugin-installations/{installed.data['id']}/connections/",
            {
                "name": "bad-connection",
                "auth_type": "credential_ref",
                "credential_ref": reference,
                "config": {},
            },
            format="json",
        )
        assert response.status_code == 400, reference
        assert PluginConnection.objects.count() == 0


@pytest.mark.django_db
def test_plugin_installation_rejects_undeclared_permissions(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    registered = register_plugin(client, tenant)
    client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:write"],
            "config": {},
        },
        format="json",
    )

    assert response.status_code == 400
    assert PluginInstallation.objects.count() == 0


@pytest.mark.django_db
def test_non_idempotent_plugin_tool_cannot_configure_retries(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    payload = plugin_payload(tenant)
    payload["tools"][0]["idempotency_mode"] = "none"
    payload["tools"][0]["retry_policy"] = {"max_attempts": 2}

    response = client.post("/api/v1/connectors/", payload, format="json")

    assert response.status_code == 400
    assert PluginDefinition.objects.count() == 0


@pytest.mark.django_db
def test_plugin_installation_version_is_pinned(plugin_workspace):
    user, tenant, project, client = plugin_workspace
    registered = register_plugin(client, tenant)
    client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    installation = PluginInstallation.objects.get(pk=installed.data["id"])
    installation.plugin_version = "0.9.0"
    installation.save(update_fields=["plugin_version"])

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_plugin_upgrade_requires_uninstalling_the_previous_project_version(
    plugin_workspace,
):
    user, tenant, project, client = plugin_workspace
    first = register_plugin(client, tenant)
    client.post(
        f"/api/v1/connectors/{first.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": first.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    next_payload = plugin_payload(tenant)
    next_payload["version"] = "1.1.0"
    next_version = client.post("/api/v1/connectors/", next_payload, format="json")
    client.post(
        f"/api/v1/connectors/{next_version.data['id']}/approve/",
        {},
        format="json",
    )

    blocked = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": next_version.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    client.post(
        f"/api/v1/plugin-installations/{installed.data['id']}/status/",
        {"status": "uninstalled"},
        format="json",
    )
    upgraded = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": next_version.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )

    assert blocked.status_code == 409
    assert upgraded.status_code == 201
    assert upgraded.data["plugin_version"] == "1.1.0"


@pytest.mark.django_db
def test_http_plugin_connection_health_check_activates_connection(
    plugin_workspace,
):
    from unittest.mock import patch

    _, tenant, project, client = plugin_workspace
    payload = plugin_payload(tenant)
    payload["manifest"] = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": False,
            "health_check": {
                "method": "POST",
                "path": "/search",
                "path_params": {},
                "query_params": {},
                "body_param": "request",
                "read_only": True,
                "arguments": {"request": {"textQuery": "coffee"}},
            },
            "operations": {},
        }
    }
    registered = client.post("/api/v1/connectors/", payload, format="json")
    approved = client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    connection = client.post(
        f"/api/v1/plugin-installations/{installed.data['id']}/connections/",
        {
            "name": "github-readonly",
            "auth_type": "none",
            "credential_ref": "",
            "config": {},
        },
        format="json",
    )

    assert approved.status_code == 200
    assert installed.status_code == 201
    assert connection.status_code == 201
    assert connection.data["status"] == PluginConnection.Status.PENDING

    with patch(
        "core.plugin_views.execute_http_operation",
        return_value={"healthy": True},
    ) as health_check:
        response = client.post(
            f"/api/v1/plugin-connections/{connection.data['id']}/test/",
            {},
            format="json",
        )

    assert response.status_code == 200
    assert health_check.call_count == 1
    assert health_check.call_args.args[0] == {"request": {"textQuery": "coffee"}}
    assert health_check.call_args.args[2].idempotency_mode == "read_only"
    connection_record = PluginConnection.objects.get(pk=connection.data["id"])
    assert connection_record.status == PluginConnection.Status.HEALTHY
    assert connection_record.last_health_check_at is not None


@pytest.mark.django_db
def test_http_plugin_connection_health_check_marks_failure(
    plugin_workspace,
):
    from unittest.mock import patch

    _, tenant, project, client = plugin_workspace
    payload = plugin_payload(tenant)
    payload["manifest"] = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": False,
            "health_check": {"method": "GET", "path": "/health"},
            "operations": {},
        }
    }
    registered = client.post("/api/v1/connectors/", payload, format="json")
    client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    connection = client.post(
        f"/api/v1/plugin-installations/{installed.data['id']}/connections/",
        {
            "name": "github-failed",
            "auth_type": "none",
            "credential_ref": "",
            "config": {},
        },
        format="json",
    )

    with patch(
        "core.plugin_views.execute_http_operation",
        side_effect=ToolExecutionError("Connector host is not allowed."),
    ):
        response = client.post(
            f"/api/v1/plugin-connections/{connection.data['id']}/test/",
            {},
            format="json",
        )

    assert response.status_code == 502
    connection_obj = PluginConnection.objects.get(pk=connection.data["id"])
    assert connection_obj.status == PluginConnection.Status.ERROR


@pytest.mark.django_db
def test_plugin_connection_can_only_be_disabled_or_reenabled_pending(plugin_workspace):
    _, tenant, project, client = plugin_workspace
    payload = plugin_payload(tenant)
    payload["manifest"] = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": False,
            "health_check": {"method": "GET", "path": "/health"},
            "operations": {},
        }
    }
    registered = client.post("/api/v1/connectors/", payload, format="json")
    client.post(
        f"/api/v1/connectors/{registered.data['id']}/approve/",
        {},
        format="json",
    )
    installed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/plugin-installations/",
        {
            "plugin_id": registered.data["id"],
            "granted_permissions": ["github:repo:read"],
            "config": {},
        },
        format="json",
    )
    connection = client.post(
        f"/api/v1/plugin-installations/{installed.data['id']}/connections/",
        {
            "name": "github-toggle",
            "auth_type": "none",
            "credential_ref": "",
            "config": {},
        },
        format="json",
    )
    connection_id = connection.data["id"]

    disabled = client.post(
        f"/api/v1/plugin-connections/{connection_id}/status/",
        {"status": "disabled"},
        format="json",
    )
    reenabled = client.post(
        f"/api/v1/plugin-connections/{connection_id}/status/",
        {"status": "pending"},
        format="json",
    )
    forced_healthy = client.post(
        f"/api/v1/plugin-connections/{connection_id}/status/",
        {"status": "healthy"},
        format="json",
    )

    assert disabled.status_code == 200
    assert disabled.data["connections"][0]["status"] == "disabled"
    assert reenabled.status_code == 200
    assert reenabled.data["connections"][0]["status"] == "pending"
    assert forced_healthy.status_code == 400
