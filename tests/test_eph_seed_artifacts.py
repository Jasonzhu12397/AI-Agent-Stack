# ruff: noqa: I001
import io
import zipfile

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from django.contrib.auth import get_user_model
from core.models import EPHSeedArtifact, Project, ProjectMembership, Tenant, TenantMembership


SEED_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


@pytest.fixture
def seed_workspace(db, settings):
    settings.EPH_SEED_ENCRYPTION_KEY = SEED_KEY
    user = get_user_model().objects.create_user(
        username="seed-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Seed tenant", slug="seed-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Seed project",
        slug="seed-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    enrolled = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-nodes/",
        {"name": "eph-seed-01", "node_type": "eph_node"},
        format="json",
    )
    assert enrolled.status_code == 201
    return tenant, project, client, enrolled.data


def create_seed(client, tenant, project, enrollment, **overrides):
    payload = {
        "enrollment_token": enrollment["enrollment_token"],
        "control_url": "https://control.example.com",
        "agent_image": "registry.example.com/agent@sha256:" + "a" * 64,
        "hostname": "eph-seed-01",
        "network_config": {
            "version": 2,
            "ethernets": {
                "uplink": {
                    "match": {"name": "en*"},
                    "dhcp4": True,
                    "dhcp6": False,
                }
            },
        },
        "ca_certificate": (
            "-----BEGIN CERTIFICATE-----\nCONTROL CA\n-----END CERTIFICATE-----"
        ),
        "agent_ca_certificate": (
            "-----BEGIN CERTIFICATE-----\nINDEPENDENT AGENT CA\n-----END CERTIFICATE-----"
        ),
    }
    payload.update(overrides)
    return client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
            f"agent-nodes/{enrollment['id']}/seed-artifacts/"
        ),
        payload,
        format="json",
    )


@pytest.mark.django_db
def test_seed_artifact_is_encrypted_and_downloaded_once(seed_workspace):
    tenant, project, client, enrollment = seed_workspace

    created = create_seed(client, tenant, project, enrollment)

    assert created.status_code == 201
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    assert enrollment["enrollment_token"] not in artifact.encrypted_payload
    assert "control.example.com" not in artifact.encrypted_payload
    assert artifact.downloaded_at is None
    assert artifact.revoked_at is None

    download = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )

    assert download.status_code == 200
    assert download["Content-Type"] == "application/zip"
    assert download["Cache-Control"] == "no-store, max-age=0"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert set(archive.namelist()) == {
            "user-data",
            "meta-data",
            "network-config",
        }
        user_data = archive.read("user-data").decode("utf-8")
        assert enrollment["enrollment_token"] in user_data
        assert "tls.key" not in user_data
        assert "tls.crt" not in user_data
        assert "tls/agent-ca.crt" in user_data
        assert "INDEPENDENT AGENT CA" in user_data

    artifact.refresh_from_db()
    assert artifact.downloaded_at is not None
    assert artifact.encrypted_payload == ""

    replay = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert replay.status_code == 404


@pytest.mark.django_db
def test_seed_artifact_rejects_invalid_enrollment_token(seed_workspace):
    tenant, project, client, enrollment = seed_workspace

    response = create_seed(
        client,
        tenant,
        project,
        enrollment,
        enrollment_token="x" * 64,
    )

    assert response.status_code == 401
    assert EPHSeedArtifact.objects.count() == 0


@pytest.mark.django_db
def test_seed_artifact_requires_independent_agent_issuer_ca(seed_workspace):
    tenant, project, client, enrollment = seed_workspace

    response = create_seed(
        client,
        tenant,
        project,
        enrollment,
        agent_ca_certificate=None,
    )

    assert response.status_code == 400
    assert EPHSeedArtifact.objects.count() == 0


@pytest.mark.django_db
def test_seed_artifact_requires_encryption_key(seed_workspace, settings):
    tenant, project, client, enrollment = seed_workspace
    settings.EPH_SEED_ENCRYPTION_KEY = ""

    response = create_seed(client, tenant, project, enrollment)

    assert response.status_code == 503
    assert EPHSeedArtifact.objects.count() == 0


@pytest.mark.django_db
def test_seed_artifact_cannot_be_downloaded_after_revocation(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201

    revoked = client.post(
        (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/"
            f"agent-nodes/{enrollment['id']}/seed-artifacts/"
            f"{created.data['artifact_id']}/revoke/"
        ),
        {},
        format="json",
    )

    assert revoked.status_code == 200
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    assert artifact.revoked_at is not None
    assert artifact.encrypted_payload == ""

    download = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert download.status_code == 404


@pytest.mark.django_db
def test_seed_artifact_expiry_and_download_token_are_enforced(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201

    invalid_token = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + "z" * 64,
    )
    assert invalid_token.status_code == 404

    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    artifact.expires_at = timezone.now()
    artifact.save(update_fields=["expires_at"])

    expired = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert expired.status_code == 404


@pytest.mark.django_db
def test_only_one_active_undownloaded_seed_artifact_per_node(seed_workspace):
    tenant, project, client, enrollment = seed_workspace

    first = create_seed(client, tenant, project, enrollment)
    second = create_seed(client, tenant, project, enrollment)

    assert first.status_code == 201
    assert second.status_code == 409
    assert EPHSeedArtifact.objects.filter(agent_node_id=enrollment["id"]).count() == 1


@pytest.mark.django_db
def test_seed_artifact_requires_project_administrator(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    tenant_membership = TenantMembership.objects.get(tenant=tenant)
    tenant_membership.role = TenantMembership.Role.MEMBER
    tenant_membership.save(update_fields=["role"])
    project_membership = ProjectMembership.objects.get(project=project)
    project_membership.role = ProjectMembership.Role.VIEWER
    project_membership.save(update_fields=["role"])

    response = create_seed(client, tenant, project, enrollment)

    assert response.status_code == 403



@pytest.mark.django_db
def test_node_revocation_invalidates_undownloaded_seed_artifact(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201

    revoked = client.post(
        f"/api/v1/agent-nodes/{enrollment['id']}/revoke/",
        {},
        format="json",
    )

    assert revoked.status_code == 200
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    assert artifact.revoked_at is not None
    assert artifact.encrypted_payload == ""

    download = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert download.status_code == 404


@pytest.mark.django_db
def test_credential_rotation_invalidates_undownloaded_seed_artifact(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201

    rotated = client.post(
        f"/api/v1/agent-nodes/{enrollment['id']}/credentials/rotate/",
        {},
        format="json",
    )

    assert rotated.status_code == 200
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    assert artifact.revoked_at is not None
    assert artifact.encrypted_payload == ""

    download = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert download.status_code == 404


@pytest.mark.django_db
def test_downloaded_seed_cannot_be_reissued_with_same_enrollment_token(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201
    download = APIClient().get(
        created.data["download_url"],
        HTTP_AUTHORIZATION="Bearer " + created.data["download_token"],
    )
    assert download.status_code == 200

    second = create_seed(client, tenant, project, enrollment)

    assert second.status_code == 409
