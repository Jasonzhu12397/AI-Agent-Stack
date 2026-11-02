import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def user_factory(db):
    user_model = get_user_model()

    def create_user(username):
        return user_model.objects.create_user(username=username, password="test-password")

    return create_user


@pytest.mark.django_db
def test_unauthenticated_user_cannot_list_tenants():
    response = APIClient().get("/api/v1/tenants/")
    assert response.status_code == 403


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


@pytest.mark.django_db
def test_only_tenant_owner_or_admin_can_create_project(user_factory):
    owner = user_factory("project-owner")
    member = user_factory("project-member")
    tenant = Tenant.objects.create(name="Create tenant", slug="create-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")

    client = APIClient()
    client.force_authenticate(user=member)
    denied = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/",
        {"name": "Denied", "slug": "denied"},
        format="json",
    )
    assert denied.status_code == 403

    client.force_authenticate(user=owner)
    allowed = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/",
        {"name": "Allowed", "slug": "allowed"},
        format="json",
    )
    assert allowed.status_code == 201
    created = Project.objects.get(tenant=tenant, slug="allowed")
    assert ProjectMembership.objects.filter(
        project=created,
        user=owner,
        role=ProjectMembership.Role.ADMIN,
    ).exists()


@pytest.mark.django_db
def test_project_creation_ignores_spoofed_tenant_id(user_factory):
    user = user_factory("project-spoof-owner")
    tenant = Tenant.objects.create(name="Authorized", slug="authorized-tenant")
    foreign = Tenant.objects.create(name="Foreign", slug="foreign-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="admin")

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/",
        {"name": "Scoped project", "slug": "scoped", "tenant_id": str(foreign.id)},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["tenant_id"] == str(tenant.id)
    assert not Project.objects.filter(tenant=foreign, slug="scoped").exists()


@pytest.mark.django_db
def test_project_slug_must_be_unique_within_tenant(user_factory):
    user = user_factory("project-slug-owner")
    tenant = Tenant.objects.create(name="Slug tenant", slug="slug-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    Project.objects.create(tenant=tenant, name="Existing", slug="duplicate")

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/",
        {"name": "Duplicate", "slug": "duplicate"},
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_authenticated_user_can_create_tenant_and_become_owner(user_factory):
    user = user_factory("tenant-creator")
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        "/api/v1/tenants/",
        {"name": "Created tenant", "slug": "created-tenant"},
        format="json",
    )

    assert response.status_code == 201
    tenant = Tenant.objects.get(slug="created-tenant")
    assert response.data["id"] == str(tenant.id)
    assert TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    ).exists()
