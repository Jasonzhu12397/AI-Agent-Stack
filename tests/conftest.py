import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


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
