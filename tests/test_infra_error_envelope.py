"""Every infrastructure write endpoint must answer with the shared envelope.

These views used to return ``{"detail": message}`` while billing and settings
returned ``{"error": {"code": ...}}``, so a browser could not branch on
``error.code`` and had to string-match the message. The probe below pins the
contract for the resources whose validation used to leak a bare ``detail``.
"""

import pytest
from core.models import (
    AgentNode,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def console():
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="envelope-owner")
    tenant = Tenant.objects.create(name="Envelope", slug="envelope")
    TenantMembership.objects.create(tenant=tenant, user=owner, role="owner")
    project = Project.objects.create(tenant=tenant, name="Envelope", slug="envelope-project")
    ProjectMembership.objects.create(project=project, user=owner, role="admin")
    AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=owner,
        name="envelope-node",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="1" * 64,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    return client, str(tenant.id), str(project.id)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "members/",
        "projects/{project}/members/",
        "projects/{project}/agent-nodes/",
        "projects/{project}/cluster-deployments/",
        "projects/{project}/knowledge-bases/",
        "projects/{project}/plugin-installations/",
    ],
)
def test_empty_post_returns_error_envelope(console, path):
    client, tenant_id, project_id = console
    url = f"/api/v1/tenants/{tenant_id}/{path.format(project=project_id)}"

    response = client.post(url, {}, format="json")

    assert response.status_code == 400
    error = response.data["error"]
    assert error["code"].isupper()
    assert error["message"]
    assert "detail" not in response.data


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "projects/{project}/infrastructure-networks/",
        "projects/{project}/fleet/machines/",
        "token-budget/",
    ],
)
def test_unsupported_method_returns_error_envelope(console, path):
    client, tenant_id, project_id = console
    url = f"/api/v1/tenants/{tenant_id}/{path.format(project=project_id)}"

    response = client.post(url, {}, format="json")

    assert response.status_code == 405
    assert response.data["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "detail" not in response.data
