"""Membership APIs speak the shared contract error envelope.

Membership management used to answer with DRF ``{"detail": ...}`` while the
settings/billing surfaces had already moved to
``{"error": {"code", "message", "details"}}``. These tests pin the unified
shape so a client can branch on ``error.code`` everywhere and no endpoint
regresses to ``detail``.
"""

import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

ERROR_KEYS = {"code", "message", "details"}


def _setup():
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="member-owner")
    admin = user_model.objects.create_user(username="member-admin")
    member = user_model.objects.create_user(username="member-plain")
    outsider = user_model.objects.create_user(username="member-outsider")
    tenant = Tenant.objects.create(name="Membership", slug="membership")
    TenantMembership.objects.create(tenant=tenant, user=owner, role=TenantMembership.Role.OWNER)
    TenantMembership.objects.create(tenant=tenant, user=admin, role=TenantMembership.Role.ADMIN)
    TenantMembership.objects.create(tenant=tenant, user=member, role=TenantMembership.Role.MEMBER)
    project = Project.objects.create(tenant=tenant, name="Project", slug="project")
    ProjectMembership.objects.create(
        project=project, user=owner, role=ProjectMembership.Role.ADMIN
    )
    ProjectMembership.objects.create(
        project=project, user=member, role=ProjectMembership.Role.EDITOR
    )
    return owner, admin, member, outsider, tenant, project


def _tenant_url(tenant_id):
    return f"/api/v1/tenants/{tenant_id}/members/"


def _tenant_member_url(tenant_id, user_id):
    return f"/api/v1/tenants/{tenant_id}/members/{user_id}/"


def _project_url(tenant_id, project_id):
    return f"/api/v1/tenants/{tenant_id}/projects/{project_id}/members/"


def _assert_envelope(response, http_status, code):
    assert response.status_code == http_status
    assert "detail" not in response.data
    assert set(response.data["error"]) == ERROR_KEYS
    assert response.data["error"]["code"] == code
    assert response.data["error"]["message"]
    assert isinstance(response.data["error"]["details"], dict)


@pytest.mark.django_db
def test_non_admin_tenant_list_is_denied_with_the_envelope():
    _owner, _admin, member, _outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.get(_tenant_url(tenant.id))

    _assert_envelope(response, 403, "TENANT_ADMIN_REQUIRED")


@pytest.mark.django_db
def test_invalid_member_input_is_a_validation_envelope():
    owner, _admin, _member, _outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _tenant_url(tenant.id), {"username": "", "role": "member"}, format="json"
    )

    _assert_envelope(response, 400, "MEMBERSHIP_INPUT_INVALID")


@pytest.mark.django_db
def test_duplicate_member_is_a_conflict_envelope():
    owner, _admin, member, _outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _tenant_url(tenant.id), {"username": member.username, "role": "member"}, format="json"
    )

    _assert_envelope(response, 409, "MEMBERSHIP_EXISTS")


@pytest.mark.django_db
def test_granting_owner_requires_an_owner_envelope_code():
    _owner, admin, _member, outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=admin)

    response = client.post(
        _tenant_url(tenant.id), {"username": outsider.username, "role": "owner"}, format="json"
    )

    _assert_envelope(response, 403, "OWNER_ROLE_REQUIRED")


@pytest.mark.django_db
def test_removing_the_last_owner_is_protected_with_the_envelope():
    owner, _admin, _member, _outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=owner)

    membership = TenantMembership.objects.get(tenant=tenant, user=owner)
    response = client.delete(_tenant_member_url(tenant.id, membership.pk))

    _assert_envelope(response, 409, "LAST_OWNER_PROTECTED")


@pytest.mark.django_db
def test_project_member_requires_tenant_membership_envelope():
    owner, _admin, _member, outsider, tenant, project = _setup()
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        _project_url(tenant.id, project.id),
        {"username": outsider.username, "role": "editor"},
        format="json",
    )

    _assert_envelope(response, 409, "PROJECT_MEMBER_NOT_IN_TENANT")


@pytest.mark.django_db
def test_project_member_view_is_denied_for_a_non_member_with_the_envelope():
    _owner, _admin, _member, outsider, tenant, project = _setup()
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.get(_project_url(tenant.id, project.id))

    # Tenant isolation wins: a stranger gets 404, still in envelope form.
    _assert_envelope(response, 404, "NOT_FOUND")


@pytest.mark.django_db
def test_membership_success_payload_stays_plain():
    owner, _admin, member, _outsider, tenant, _project = _setup()
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.get(_tenant_url(tenant.id))

    assert response.status_code == 200
    assert isinstance(response.data, list)
    assert "error" not in response.data
