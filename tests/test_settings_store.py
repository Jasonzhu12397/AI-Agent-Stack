"""Server-side settings storage: auth, isolation, roles, validation, precedence."""

import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def actor_factory(db):
    user_model = get_user_model()

    def create(username, email=None):
        return user_model.objects.create_user(
            username=username,
            password="test-password",
            email=email or f"{username}@example.test",
        )

    return create


def _envelope(response):
    assert "error" in response.data, response.data
    error = response.data["error"]
    assert {"code", "message", "details"} <= set(error)
    return error


@pytest.mark.django_db
def test_settings_requires_auth(actor_factory):
    tenant = Tenant.objects.create(name="Auth tenant", slug="auth-tenant")
    client = APIClient()

    response = client.get(f"/api/v1/tenants/{tenant.id}/settings/general/")

    assert response.status_code in {401, 403}
    error = _envelope(response)
    assert error["code"] in {"AUTH_REQUIRED", "FORBIDDEN"}


@pytest.mark.django_db
def test_settings_cross_tenant_isolation(actor_factory):
    outsider = actor_factory("isol-outsider")
    insider = actor_factory("isol-insider")
    own_tenant = Tenant.objects.create(name="Own", slug="isol-own")
    foreign_tenant = Tenant.objects.create(name="Foreign", slug="isol-foreign")
    TenantMembership.objects.create(tenant=own_tenant, user=insider, role="owner")
    TenantMembership.objects.create(tenant=foreign_tenant, user=outsider, role="owner")

    client = APIClient()
    client.force_authenticate(user=insider)

    read = client.get(f"/api/v1/tenants/{foreign_tenant.id}/settings/general/")
    assert read.status_code == 404
    _envelope(read)

    write = client.patch(
        f"/api/v1/tenants/{foreign_tenant.id}/settings/general/",
        {"values": {"compact_mode": True}},
        format="json",
    )
    assert write.status_code == 404
    _envelope(write)


@pytest.mark.django_db
def test_settings_forbidden_role(actor_factory):
    owner = actor_factory("role-owner")
    member = actor_factory("role-member")
    tenant = Tenant.objects.create(name="Role tenant", slug="role-tenant")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")

    member_client = APIClient()
    member_client.force_authenticate(user=member)

    readable = member_client.get(f"/api/v1/tenants/{tenant.id}/settings/general/")
    assert readable.status_code == 200

    denied = member_client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"compact_mode": True}},
        format="json",
    )
    assert denied.status_code == 403
    error = _envelope(denied)
    assert error["code"] == "FORBIDDEN"


@pytest.mark.django_db
def test_settings_unknown_key_rejected(actor_factory):
    owner = actor_factory("unknown-key-owner")
    tenant = Tenant.objects.create(name="Unknown key", slug="unknown-key")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"definitely_not_a_setting": True}},
        format="json",
    )

    assert response.status_code == 400
    error = _envelope(response)
    assert error["code"] == "SETTINGS_UNKNOWN_KEY"
    assert "definitely_not_a_setting" in error["details"]


@pytest.mark.django_db
def test_settings_invalid_value_details(actor_factory):
    owner = actor_factory("invalid-value-owner")
    tenant = Tenant.objects.create(name="Invalid value", slug="invalid-value")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    client = APIClient()
    client.force_authenticate(user=owner)

    wrong_type = client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"items_per_page": "not-a-number"}},
        format="json",
    )
    assert wrong_type.status_code == 400
    error = _envelope(wrong_type)
    assert error["code"] == "SETTINGS_INVALID_VALUE"
    assert "items_per_page" in error["details"]

    out_of_range = client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"items_per_page": 99999}},
        format="json",
    )
    assert out_of_range.status_code == 400
    error = _envelope(out_of_range)
    assert error["code"] == "SETTINGS_INVALID_VALUE"
    assert "items_per_page" in error["details"]


@pytest.mark.django_db
def test_settings_precedence_project_over_tenant(actor_factory):
    owner = actor_factory("precedence-owner")
    tenant = Tenant.objects.create(name="Precedence", slug="precedence")
    project = Project.objects.create(tenant=tenant, name="Precedence project", slug="precedence-proj")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    ProjectMembership.objects.create(project=project, user=owner, role="admin")
    client = APIClient()
    client.force_authenticate(user=owner)

    tenant_patch = client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"items_per_page": 50}},
        format="json",
    )
    assert tenant_patch.status_code == 200

    project_patch = client.patch(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/settings/general/",
        {"values": {"items_per_page": 10}},
        format="json",
    )
    assert project_patch.status_code == 200

    project_view = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/settings/general/"
    )
    assert project_view.status_code == 200
    assert project_view.data["effective"]["items_per_page"] == 10
    assert project_view.data["sources"]["items_per_page"] == "project"

    tenant_view = client.get(f"/api/v1/tenants/{tenant.id}/settings/general/")
    assert tenant_view.status_code == 200
    assert tenant_view.data["effective"]["items_per_page"] == 50
    assert tenant_view.data["sources"]["items_per_page"] == "tenant"


@pytest.mark.django_db
def test_settings_user_local_overrides(actor_factory):
    owner = actor_factory("local-override-owner")
    tenant = Tenant.objects.create(name="Local override", slug="local-override")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    client = APIClient()
    client.force_authenticate(user=owner)

    client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/notifications/",
        {"values": {"mention_alerts": False}},
        format="json",
    )

    appearance = client.put(
        "/api/v1/users/me/preferences/appearance/",
        {"values": {"theme": "dark"}},
        format="json",
    )
    assert appearance.status_code == 200
    assert appearance.data["effective"]["theme"] == "dark"
    assert appearance.data["sources"]["theme"] == "user"

    delivery = client.patch(
        "/api/v1/users/me/preferences/notification-delivery/",
        {"values": {"in_app": True, "digest_frequency": "daily"}},
        format="json",
    )
    assert delivery.status_code == 200
    assert delivery.data["effective"]["in_app"] is True
    assert delivery.data["sources"]["in_app"] == "user"


@pytest.mark.django_db
def test_settings_persist_across_new_client(actor_factory):
    owner = actor_factory("persist-owner")
    tenant = Tenant.objects.create(name="Persist", slug="persist")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")

    first = APIClient()
    first.force_authenticate(user=owner)
    patched = first.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"compact_mode": True}},
        format="json",
    )
    assert patched.status_code == 200

    second = APIClient()
    second.force_authenticate(user=owner)
    reopened = second.get(f"/api/v1/tenants/{tenant.id}/settings/general/")
    assert reopened.status_code == 200
    assert reopened.data["effective"]["compact_mode"] is True


@pytest.mark.django_db
def test_settings_error_envelope_shape(actor_factory):
    owner = actor_factory("envelope-owner")
    member = actor_factory("envelope-member")
    tenant = Tenant.objects.create(name="Envelope", slug="envelope")
    foreign = Tenant.objects.create(name="Envelope foreign", slug="envelope-foreign")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")

    anon = APIClient()
    unauthenticated = anon.get(f"/api/v1/tenants/{tenant.id}/settings/general/")
    assert unauthenticated.status_code in {401, 403}
    _envelope(unauthenticated)

    member_client = APIClient()
    member_client.force_authenticate(user=member)
    forbidden = member_client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"compact_mode": True}},
        format="json",
    )
    assert forbidden.status_code == 403
    _envelope(forbidden)

    owner_client = APIClient()
    owner_client.force_authenticate(user=owner)
    missing = owner_client.get(f"/api/v1/tenants/{foreign.id}/settings/general/")
    assert missing.status_code == 404
    _envelope(missing)

    bad = owner_client.patch(
        f"/api/v1/tenants/{tenant.id}/settings/general/",
        {"values": {"nope": 1}},
        format="json",
    )
    assert bad.status_code == 400
    _envelope(bad)


@pytest.mark.django_db
def test_user_preferences_defaults_deterministic(actor_factory):
    first = actor_factory("defaults-first")
    second = actor_factory("defaults-second")

    client_one = APIClient()
    client_one.force_authenticate(user=first)
    view_one = client_one.get("/api/v1/users/me/preferences/appearance/")
    assert view_one.status_code == 200

    client_two = APIClient()
    client_two.force_authenticate(user=second)
    view_two = client_two.get("/api/v1/users/me/preferences/appearance/")
    assert view_two.status_code == 200

    assert view_one.data["defaults"] == view_two.data["defaults"]
    assert view_one.data["effective"] == view_one.data["defaults"]
    assert view_two.data["effective"] == view_two.data["defaults"]
    assert all(source == "default" for source in view_one.data["sources"].values())


@pytest.mark.django_db
def test_user_preferences_cross_user_isolation(actor_factory):
    owner = actor_factory("isol-pref-owner")
    other = actor_factory("isol-pref-other")

    client_one = APIClient()
    client_one.force_authenticate(user=owner)
    updated = client_one.put(
        "/api/v1/users/me/preferences/appearance/",
        {"values": {"theme": "dark"}},
        format="json",
    )
    assert updated.status_code == 200

    client_two = APIClient()
    client_two.force_authenticate(user=other)
    view = client_two.get("/api/v1/users/me/preferences/appearance/")
    assert view.status_code == 200
    assert view.data["effective"]["theme"] == view.data["defaults"]["theme"]
