# ruff: noqa: I001
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.audit import record_audit_event, verify_audit_chain
from core.models import AuditEvent, Project, ProjectMembership, Tenant, TenantMembership


@pytest.fixture
def audit_workspace(db):
    user = get_user_model().objects.create_user(
        username="audit-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Audit tenant", slug="audit-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Audit project",
        slug="audit-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    request = SimpleNamespace(
        user=user,
        headers={"X-Request-ID": "audit-test-request", "User-Agent": "pytest"},
        META={"REMOTE_ADDR": "127.0.0.1"},
    )
    return user, tenant, project, client, request


@pytest.mark.django_db
def test_audit_events_form_a_tamper_evident_chain_and_filter_sensitive_metadata(
    audit_workspace,
):
    user, tenant, project, _, request = audit_workspace
    first = record_audit_event(
        request,
        tenant,
        "agent.command.approved",
        "agent_command",
        "command-1",
        project=project,
        metadata={
            "command_type": "cluster.provision",
            "plan_hash": "a" * 64,
            "api_token": "must-not-be-stored",
            "nested": {"password": "must-not-be-stored", "node_name": "eph-01"},
        },
    )
    second = record_audit_event(
        request,
        tenant,
        "cluster_deployment.created",
        "cluster_deployment",
        "deployment-1",
        project=project,
        metadata={"cluster_name": "edge-lab"},
    )

    first.refresh_from_db()
    assert first.actor_id == user.id
    assert first.request_id == "audit-test-request"
    assert "api_token" not in first.metadata
    assert "password" not in first.metadata["nested"]
    assert first.event_hash == second.previous_hash
    with pytest.raises(ValueError, match="append-only"):
        first.save()
    with pytest.raises(ValueError, match="cannot be deleted"):
        first.delete()
    assert verify_audit_chain(tenant)["valid"] is True
    assert verify_audit_chain(tenant)["verified_count"] == 2

    AuditEvent.objects.filter(pk=first.pk).update(
        metadata={"command_type": "tampered"},
    )
    verification = verify_audit_chain(tenant)
    assert verification["valid"] is False
    assert verification["invalid_event_id"] == str(first.id)


@pytest.mark.django_db
def test_audit_api_is_tenant_admin_only_and_returns_chain_verification(audit_workspace):
    _, tenant, project, client, request = audit_workspace
    record_audit_event(
        request,
        tenant,
        "connector.approved",
        "plugin_definition",
        "plugin-1",
        project=project,
        metadata={"plugin_key": "sample"},
    )

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/audit-events/?project_id={project.id}",
    )
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["event_type"] == "connector.approved"
    assert response.data["results"][0]["event_hash"]

    verification = client.get(
        f"/api/v1/tenants/{tenant.id}/audit-events/verify/",
    )
    assert verification.status_code == 200
    assert verification.data["valid"] is True
    assert verification.data["verified_count"] == 1


@pytest.mark.django_db
def test_audit_api_denies_non_admin_tenant_member(audit_workspace):
    _, tenant, _, _, _ = audit_workspace
    member = get_user_model().objects.create_user(
        username="audit-member",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=member,
        role=TenantMembership.Role.MEMBER,
    )
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.get(f"/api/v1/tenants/{tenant.id}/audit-events/")
    assert response.status_code == 403
