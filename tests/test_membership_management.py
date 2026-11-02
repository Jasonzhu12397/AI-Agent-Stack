import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def users(db):
    user_model = get_user_model()

    def create(username):
        return user_model.objects.create_user(username=username, password="test-password")

    return create


@pytest.mark.django_db
def test_owner_can_add_tenant_member_and_admin_cannot_grant_owner(users):
    owner = users("membership-owner")
    admin = users("membership-admin")
    member = users("membership-new")
    tenant = Tenant.objects.create(name="Membership", slug="membership")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="admin")
    client = APIClient()
    client.force_authenticate(user=admin)

    denied = client.post(
        f"/api/v1/tenants/{tenant.id}/members/",
        {"username": member.username, "role": "owner"},
        format="json",
    )
    assert denied.status_code == 403

    allowed = client.post(
        f"/api/v1/tenants/{tenant.id}/members/",
        {"username": member.username, "role": "member"},
        format="json",
    )
    assert allowed.status_code == 201
    assert TenantMembership.objects.filter(tenant=tenant, user=member, role="member").exists()


@pytest.mark.django_db
def test_last_tenant_owner_cannot_be_demoted_or_removed(users):
    owner = users("last-owner")
    tenant = Tenant.objects.create(name="Owner protection", slug="owner-protection")
    membership = TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    client = APIClient()
    client.force_authenticate(user=owner)

    demoted = client.patch(
        f"/api/v1/tenants/{tenant.id}/members/{membership.id}/",
        {"role": "admin"},
        format="json",
    )
    removed = client.delete(f"/api/v1/tenants/{tenant.id}/members/{membership.id}/")

    assert demoted.status_code == 409
    assert removed.status_code == 409
    assert TenantMembership.objects.filter(pk=membership.pk, role="owner").exists()


@pytest.mark.django_db
def test_tenant_admin_cannot_modify_or_remove_owner(users):
    owner = users("protected-owner")
    admin = users("protected-admin")
    tenant = Tenant.objects.create(name="Protected", slug="protected")
    owner_membership = TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="admin")
    client = APIClient()
    client.force_authenticate(user=admin)

    changed = client.patch(
        f"/api/v1/tenants/{tenant.id}/members/{owner_membership.id}/",
        {"role": "member"},
        format="json",
    )
    removed = client.delete(f"/api/v1/tenants/{tenant.id}/members/{owner_membership.id}/")

    assert changed.status_code == 403
    assert removed.status_code == 403
    assert TenantMembership.objects.filter(pk=owner_membership.pk).exists()


@pytest.mark.django_db
def test_project_admin_can_manage_members_only_within_same_tenant(users):
    admin = users("project-manager")
    member = users("project-new-member")
    outsider = users("project-outsider")
    tenant = Tenant.objects.create(name="Project access", slug="project-access")
    foreign_tenant = Tenant.objects.create(name="Foreign access", slug="foreign-access")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="member")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    TenantMembership.objects.create(tenant=foreign_tenant, user=outsider, role="owner")
    project = Project.objects.create(tenant=tenant, name="Managed", slug="managed")
    ProjectMembership.objects.create(project=project, user=admin, role="admin")
    client = APIClient()
    client.force_authenticate(user=admin)

    added = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/",
        {"username": member.username, "role": "editor"},
        format="json",
    )
    denied = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/",
        {"username": outsider.username, "role": "viewer"},
        format="json",
    )

    assert added.status_code == 201
    assert denied.status_code == 409
    assert ProjectMembership.objects.filter(project=project, user=member, role="editor").exists()


@pytest.mark.django_db
def test_non_admin_cannot_manage_project_members(users):
    member = users("project-ordinary-member")
    new_member = users("project-ordinary-new")
    tenant = Tenant.objects.create(name="Project RBAC", slug="project-rbac")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    TenantMembership.objects.create(tenant=tenant, user=new_member, role="member")
    project = Project.objects.create(tenant=tenant, name="Restricted", slug="restricted")
    ProjectMembership.objects.create(project=project, user=member, role="viewer")
    client = APIClient()
    client.force_authenticate(user=member)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/",
        {"username": new_member.username, "role": "editor"},
        format="json",
    )
    assert response.status_code == 403



@pytest.mark.django_db
def test_tenant_admin_can_manage_projects_without_project_membership(users):
    admin = users("tenant-wide-admin")
    tenant = Tenant.objects.create(name="Tenant-wide", slug="tenant-wide")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="admin")
    project = Project.objects.create(tenant=tenant, name="Unassigned", slug="unassigned")
    client = APIClient()
    client.force_authenticate(user=admin)

    listing = client.get(f"/api/v1/tenants/{tenant.id}/projects/")
    members = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/"
    )

    assert listing.status_code == 200
    assert listing.data["count"] == 1
    assert members.status_code == 200


@pytest.mark.django_db
def test_removing_tenant_member_revokes_project_membership(users):
    admin = users("tenant-revoke-admin")
    member = users("tenant-revoke-member")
    tenant = Tenant.objects.create(name="Revocation", slug="revocation")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="owner")
    tenant_membership = TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    project = Project.objects.create(tenant=tenant, name="Revoked project", slug="revoked-project")
    project_membership = ProjectMembership.objects.create(
        project=project,
        user=member,
        role="editor",
    )
    client = APIClient()
    client.force_authenticate(user=admin)

    response = client.delete(
        f"/api/v1/tenants/{tenant.id}/members/{tenant_membership.id}/"
    )

    assert response.status_code == 204
    assert not ProjectMembership.objects.filter(pk=project_membership.pk).exists()


@pytest.mark.django_db
def test_last_project_admin_cannot_be_demoted_or_removed(users):
    admin = users("only-project-admin")
    tenant = Tenant.objects.create(name="Project owner guard", slug="project-owner-guard")
    TenantMembership.objects.create(tenant=tenant, user=admin, role="member")
    project = Project.objects.create(
        tenant=tenant,
        name="Protected project",
        slug="protected-project",
    )
    membership = ProjectMembership.objects.create(project=project, user=admin, role="admin")
    client = APIClient()
    client.force_authenticate(user=admin)

    demoted = client.patch(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/{membership.id}/",
        {"role": "viewer"},
        format="json",
    )
    removed = client.delete(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/members/{membership.id}/"
    )

    assert demoted.status_code == 409
    assert removed.status_code == 409
    assert ProjectMembership.objects.filter(pk=membership.pk, role="admin").exists()
