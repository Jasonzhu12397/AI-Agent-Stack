import uuid

import pytest
from core.models import (
    AuditEvent,
    PluginConnection,
    PluginDefinition,
    PluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def connector_workspace(db):
    user = get_user_model().objects.create_user(
        username="dc-connector-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="DC Connector Tenant", slug="dc-connector-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="DC Connector Project", slug="dc-connector-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "project": project, "client": client}


def _make_plugin(workspace, *, key, installed=False, manifest=None):
    plugin = PluginDefinition.objects.create(
        tenant=workspace["tenant"],
        created_by=workspace["user"],
        plugin_key=key,
        name=f"Connector {key}",
        description="Catalog entry backed by the real plugin registry table.",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.HTTP,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
        manifest=manifest or {},
    )
    if installed:
        installation = PluginInstallation.objects.create(
            tenant=workspace["tenant"],
            project=workspace["project"],
            plugin=plugin,
            plugin_version=plugin.version,
            status=PluginInstallation.Status.INSTALLED,
            granted_permissions=["connector.read"],
            installed_by=workspace["user"],
        )
        PluginConnection.objects.create(
            installation=installation,
            name="default",
            auth_type=PluginConnection.AuthType.NONE,
            status=PluginConnection.Status.PENDING,
        )
    return plugin


@pytest.mark.django_db
def test_connector_catalog_lists_real_installations(connector_workspace):
    installed_plugin = _make_plugin(
        connector_workspace, key="installed-echo", installed=True
    )
    _make_plugin(connector_workspace, key="available-echo", installed=False)

    response = connector_workspace["client"].get(
        f"/api/v1/tenants/{connector_workspace['tenant'].id}/connectors/"
    )

    assert response.status_code == 200
    entries = {item["slug"]: item for item in response.data["results"]}
    assert set(entries) == {"installed-echo", "available-echo"}

    installed_entry = entries["installed-echo"]
    assert installed_entry["installed"] is True
    assert installed_entry["installations"], "real installation record must be exposed"
    installation = installed_entry["installations"][0]
    assert installation["plugin_id"] == str(installed_plugin.id)
    assert installation["status"] == PluginInstallation.Status.INSTALLED

    available_entry = entries["available-echo"]
    assert available_entry["installed"] is False
    assert available_entry["installations"] == []


@pytest.mark.django_db
def test_connector_install_idempotent_and_audited(connector_workspace):
    plugin = _make_plugin(connector_workspace, key="install-once")
    url = (
        f"/api/v1/tenants/{connector_workspace['tenant'].id}"
        f"/connectors/{plugin.plugin_key}/install/"
    )
    body = {
        "project_id": str(connector_workspace["project"].id),
        "granted_permissions": ["connector.read"],
        "config": {"region": "us-east"},
    }

    first = connector_workspace["client"].post(url, body, format="json")
    assert first.status_code == 201
    installation_id = first.data["id"]

    second = connector_workspace["client"].post(url, body, format="json")
    assert second.status_code == 200
    assert second.data["id"] == installation_id

    events = AuditEvent.objects.filter(
        tenant=connector_workspace["tenant"],
        event_type="connector.installation.created",
    )
    assert events.count() == 1
    assert events.first().resource_type == "connector_installation"


@pytest.mark.django_db
def test_connector_secret_must_be_reference(connector_workspace):
    plugin = _make_plugin(connector_workspace, key="secret-guard")
    install = connector_workspace["client"].post(
        f"/api/v1/tenants/{connector_workspace['tenant'].id}"
        f"/connectors/{plugin.plugin_key}/install/",
        {
            "project_id": str(connector_workspace["project"].id),
            "granted_permissions": [],
            "config": {},
        },
        format="json",
    )
    assert install.status_code == 201
    installation_id = install.data["id"]
    patch_url = (
        f"/api/v1/tenants/{connector_workspace['tenant'].id}"
        f"/installations/{installation_id}/"
    )

    plain_secret = connector_workspace["client"].patch(
        patch_url,
        {"config": {"api_key": "plain-secret-value-123"}},
        format="json",
    )
    assert plain_secret.status_code == 400
    assert plain_secret.data["error"]["code"] == "CONNECTOR_SECRET_NOT_REFERENCE"

    raw_uri = connector_workspace["client"].patch(
        patch_url,
        {"credential_ref": "https://example.com/secret-value"},
        format="json",
    )
    assert raw_uri.status_code == 400
    assert raw_uri.data["error"]["code"] == "CONNECTOR_SECRET_NOT_REFERENCE"

    accepted = connector_workspace["client"].patch(
        patch_url,
        {"credential_ref": "env://DC_TEST_TOKEN", "config": {"region": "us-east"}},
        format="json",
    )
    assert accepted.status_code == 200
    assert accepted.data["configured"] is True
    assert accepted.data["masked_hint"]
    assert "env://DC_TEST_TOKEN" not in accepted.content.decode()


@pytest.mark.django_db
def test_connector_test_invocation_reports_failure_code(connector_workspace):
    # Manifest without a usable health/test operation: the real gateway path
    # must fail closed and report a failure code; a fake connected state is
    # not acceptable.
    plugin = _make_plugin(
        connector_workspace,
        key="broken-connector",
        installed=True,
        manifest={"http": {}},
    )
    installation = PluginInstallation.objects.get(plugin=plugin)

    response = connector_workspace["client"].post(
        f"/api/v1/tenants/{connector_workspace['tenant'].id}"
        f"/installations/{installation.id}/test/",
        {},
        format="json",
    )

    assert response.status_code in {409, 502}
    assert response.data["error"]["code"]
    connection = PluginConnection.objects.get(installation=installation)
    assert connection.status != PluginConnection.Status.HEALTHY
    failure_events = AuditEvent.objects.filter(
        tenant=connector_workspace["tenant"],
        event_type__in=["connector.test.failed", "connector.health_check.failed"],
    )
    assert failure_events.exists()
