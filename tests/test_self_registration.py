import pytest
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

REGISTER_URL = "/api/v1/auth/register/"


@pytest.mark.django_db
def test_email_registration_creates_owner_tenant_and_project():
    client = APIClient()

    response = client.post(
        REGISTER_URL,
        {
            "identifier": "New.Member@Example.com",
            "password": "correct-horse-9",
            "display_name": "New Member",
        },
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    user = get_user_model().objects.get(username="new.member@example.com")
    assert body["email"] == "new.member@example.com"
    assert body["tenant_id"]
    assert body["project_id"]
    membership = TenantMembership.objects.get(user=user)
    assert membership.role == TenantMembership.Role.OWNER
    project = Project.objects.get(pk=body["project_id"])
    assert project.slug == "default"
    assert ProjectMembership.objects.get(user=user, project=project).role == (
        ProjectMembership.Role.ADMIN
    )
    assert AgentPluginDefinition.objects.filter(tenant=membership.tenant).count() >= 34
    assert AgentPluginInstallation.objects.filter(
        project=project, plugin__plugin_key="map"
    ).exists()
    assert client.get("/api/v1/auth/me/").status_code == 200


@pytest.mark.django_db
def test_phone_registration_is_accepted():
    client = APIClient()

    response = client.post(
        REGISTER_URL,
        {"identifier": "13800138000", "password": "correct-horse-9"},
        format="json",
    )

    assert response.status_code == 201
    assert get_user_model().objects.get(username="13800138000").email == ""


@pytest.mark.django_db
def test_duplicate_identity_is_rejected_case_insensitively():
    client = APIClient()
    payload = {"identifier": "duplicate@example.com", "password": "correct-horse-9"}
    assert client.post(REGISTER_URL, payload, format="json").status_code == 201

    response = client.post(
        REGISTER_URL,
        {"identifier": "DUPLICATE@example.com", "password": "correct-horse-9"},
        format="json",
    )

    assert response.status_code == 409
    assert get_user_model().objects.filter(username__iexact="duplicate@example.com").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "identifier,password",
    [
        ("not-an-email", "correct-horse-9"),
        ("12345", "correct-horse-9"),
        ("valid@example.com", "short"),
    ],
)
def test_invalid_registration_payload_is_rejected(identifier, password):
    client = APIClient()

    response = client.post(
        REGISTER_URL,
        {"identifier": identifier, "password": password},
        format="json",
    )

    assert response.status_code == 400
    assert Tenant.objects.count() == 0


@pytest.mark.django_db
@override_settings(PLATFORM_ALLOW_SELF_REGISTRATION=False)
def test_self_registration_can_be_disabled():
    response = APIClient().post(
        REGISTER_URL,
        {"identifier": "closed@example.com", "password": "correct-horse-9"},
        format="json",
    )

    assert response.status_code == 403
