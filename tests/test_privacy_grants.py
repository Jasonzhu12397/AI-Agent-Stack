import pytest
from core.models import (
    AuditEvent,
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
def privacy_workspace(db):
    user = get_user_model().objects.create_user(
        username="dc-privacy-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="DC Privacy Tenant", slug="dc-privacy-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="DC Privacy Project", slug="dc-privacy-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "project": project, "client": client}


def _make_installation(workspace, key="privacy-connector"):
    plugin = PluginDefinition.objects.create(
        tenant=workspace["tenant"],
        created_by=workspace["user"],
        plugin_key=key,
        name="Privacy Connector",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.HTTP,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
        manifest={},
    )
    return PluginInstallation.objects.create(
        tenant=workspace["tenant"],
        project=workspace["project"],
        plugin=plugin,
        plugin_version=plugin.version,
        status=PluginInstallation.Status.INSTALLED,
        granted_permissions=["connector.read", "connector.write"],
        installed_by=workspace["user"],
    )


@pytest.mark.django_db
def test_privacy_grant_revoke_idempotent_and_audited(privacy_workspace):
    installation = _make_installation(privacy_workspace)
    grants_url = f"/api/v1/tenants/{privacy_workspace['tenant'].id}/privacy/grants/"

    listing = privacy_workspace["client"].get(grants_url)
    assert listing.status_code == 200
    grants = listing.data["results"]
    assert len(grants) == 1
    grant = grants[0]
    assert grant["scopes"] == ["connector.read", "connector.write"]

    revoke_url = f"{grants_url}{grant['id']}/"
    first = privacy_workspace["client"].delete(revoke_url)
    assert first.status_code == 200
    installation.refresh_from_db()
    assert installation.status == PluginInstallation.Status.DISABLED

    second = privacy_workspace["client"].delete(revoke_url)
    assert second.status_code == 200

    events = AuditEvent.objects.filter(
        tenant=privacy_workspace["tenant"],
        event_type="privacy.grant.revoked",
    )
    assert events.count() == 1


@pytest.mark.django_db
def test_privacy_deletion_request_requires_confirm_and_scope(privacy_workspace):
    url = f"/api/v1/tenants/{privacy_workspace['tenant'].id}/privacy/deletion-requests/"

    missing_confirm = privacy_workspace["client"].post(
        url,
        {"scope": "archived_content", "idempotency_key": "dr-no-confirm"},
        format="json",
    )
    assert missing_confirm.status_code == 400
    assert missing_confirm.data["error"]["code"] == "DELETION_CONFIRMATION_REQUIRED"

    bad_scope = privacy_workspace["client"].post(
        url,
        {"scope": "phones", "confirm": True, "idempotency_key": "dr-bad-scope"},
        format="json",
    )
    assert bad_scope.status_code == 400
    assert bad_scope.data["error"]["code"] == "DELETION_SCOPE_UNSUPPORTED"

    missing_key = privacy_workspace["client"].post(
        url, {"scope": "exports", "confirm": True}, format="json"
    )
    assert missing_key.status_code == 400

    created = privacy_workspace["client"].post(
        url,
        {"scope": "archived_content", "confirm": True, "idempotency_key": "dr-1"},
        format="json",
    )
    assert created.status_code == 201
    assert created.data["status"] == "requested"

    listing = privacy_workspace["client"].get(url)
    assert listing.status_code == 200
    entry = next(item for item in listing.data["results"] if item["id"] == created.data["id"])
    assert entry["scope"] == "archived_content"
    assert entry["audit_ref"]


@pytest.mark.django_db
def test_privacy_sessions_lists_current_user_sessions(privacy_workspace):
    response = privacy_workspace["client"].get(
        f"/api/v1/tenants/{privacy_workspace['tenant'].id}/privacy/sessions/"
    )
    assert response.status_code == 200
    assert response.data["results"] == []


@pytest.mark.django_db
def test_data_controls_cross_tenant_isolation(privacy_workspace):
    installation = _make_installation(privacy_workspace, key="isolated-connector")

    other_user = get_user_model().objects.create_user(
        username="dc-other-owner", password="test-password"
    )
    other_tenant = Tenant.objects.create(name="Other Tenant", slug="dc-other-tenant")
    TenantMembership.objects.create(
        tenant=other_tenant, user=other_user, role=TenantMembership.Role.OWNER
    )
    other_client = APIClient()
    other_client.force_authenticate(user=other_user)

    connectors = other_client.get(
        f"/api/v1/tenants/{privacy_workspace['tenant'].id}/connectors/"
    )
    assert connectors.status_code == 404

    grants = other_client.get(
        f"/api/v1/tenants/{privacy_workspace['tenant'].id}/privacy/grants/"
    )
    assert grants.status_code == 404

    exports = other_client.get(
        f"/api/v1/tenants/{privacy_workspace['tenant'].id}/data-controls/exports/"
    )
    assert exports.status_code == 404

    grant_revoke = other_client.delete(
        f"/api/v1/tenants/{privacy_workspace['tenant'].id}/privacy/grants/{installation.id}/"
    )
    assert grant_revoke.status_code == 404
