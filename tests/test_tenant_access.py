import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Project, ProjectMembership, Tenant, TenantMembership


@pytest.fixture
def user_factory(db):
    user_model = get_user_model()

    def create_user(username):
        return user_model.objects.create_user(username=username, password="test-password")

    return create_user


@pytest.mark.django_db
def test_tenant_list_is_scoped_to_authenticated_membership(user_factory):
    user = user_factory("tenant-user")
    other_user = user_factory("other-user")
    own_tenant = Tenant.objects.create(name="Own tenant", slug="own")
    foreign_tenant = Tenant.objects.create(name="Foreign tenant", slug="foreign")
    TenantMembership.objects.create(tenant=own_tenant, user=user, role="owner")
    TenantMembership.objects.create(tenant=foreign_tenant, user=other_user, role="owner")

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get("/api/v1/tenants/")

    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [str(own_tenant.id)]


@pytest.mark.django_db
def test_client_tenant_id_parameter_cannot_expand_membership_scope(user_factory):
    user = user_factory("scope-user")
    other_user = user_factory("scope-other")
    own_tenant = Tenant.objects.create(name="Own", slug="scope-own")
    foreign_tenant = Tenant.objects.create(name="Foreign", slug="scope-foreign")
    TenantMembership.objects.create(tenant=own_tenant, user=user)
    TenantMembership.objects.create(tenant=foreign_tenant, user=other_user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/?tenant_id={foreign_tenant.id}")

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["id"] == str(own_tenant.id)


@pytest.mark.django_db
def test_project_list_requires_tenant_membership_and_project_membership(user_factory):
    user = user_factory("project-user")
    other_user = user_factory("project-other")
    tenant = Tenant.objects.create(name="Tenant", slug="project-tenant")
    foreign_tenant = Tenant.objects.create(name="Foreign", slug="project-foreign")
    TenantMembership.objects.create(tenant=tenant, user=user)
    project = Project.objects.create(tenant=tenant, name="Visible", slug="visible")
    hidden_project = Project.objects.create(tenant=tenant, name="Hidden", slug="hidden")
    foreign_project = Project.objects.create(tenant=foreign_tenant, name="Foreign", slug="foreign")
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    ProjectMembership.objects.create(project=hidden_project, user=other_user)
    ProjectMembership.objects.create(project=foreign_project, user=other_user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/")

    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [str(project.id)]


@pytest.mark.django_db
def test_cross_tenant_project_lookup_returns_not_found(user_factory):
    user = user_factory("cross-tenant-user")
    other_user = user_factory("cross-tenant-owner")
    tenant = Tenant.objects.create(name="Foreign tenant", slug="cross-foreign")
    TenantMembership.objects.create(tenant=tenant, user=other_user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_membership_revocation_takes_effect_immediately(user_factory):
    user = user_factory("revoked-user")
    tenant = Tenant.objects.create(name="Revoked tenant", slug="revoked")
    membership = TenantMembership.objects.create(tenant=tenant, user=user)
    project = Project.objects.create(tenant=tenant, name="Project", slug="revoked-project")
    ProjectMembership.objects.create(project=project, user=user)

    client = APIClient()
    client.force_authenticate(user=user)
    assert client.get(f"/api/v1/tenants/{tenant.id}/projects/").status_code == 200

    membership.delete()
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_tenant_pagination_count_does_not_include_other_users_tenants(user_factory):
    user = user_factory("paged-user")
    other_user = user_factory("paged-other")
    for index in range(12):
        tenant = Tenant.objects.create(name=f"Own {index:02d}", slug=f"paged-own-{index}")
        TenantMembership.objects.create(tenant=tenant, user=user)
    foreign = Tenant.objects.create(name="Foreign", slug="paged-foreign")
    TenantMembership.objects.create(tenant=foreign, user=other_user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get("/api/v1/tenants/?page_size=5")

    assert response.status_code == 200
    assert response.data["count"] == 12
    assert len(response.data["results"]) == 5
