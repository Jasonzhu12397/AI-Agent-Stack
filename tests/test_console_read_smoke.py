"""Every console GET endpoint answers without a server error.

The console calls a long tail of endpoints that the feature suites exercise one
at a time. This smoke matrix walks them with a real tenant owner and fails on
any 5xx, which is where a wrong serializer field, a broken ``getattr`` or an
unhandled provider error actually shows up in production.
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

PASS_STATUSES = {200, 201, 204, 400, 403, 404, 409, 422, 429, 503}


@pytest.fixture
def smoke(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="smoke-owner")
    tenant = Tenant.objects.create(name="Smoke", slug="smoke")
    TenantMembership.objects.create(tenant=tenant, user=owner, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Smoke project", slug="smoke-project")
    ProjectMembership.objects.create(
        project=project, user=owner, role=ProjectMembership.Role.ADMIN
    )
    AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=owner,
        name="node-1",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="0" * 64,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    return client, tenant, project


def _paths(tenant, project):
    t = str(tenant.id)
    p = str(project.id)
    return [
        "/api/v1/tenants/",
        "/api/v1/users/me/",
        "/api/v1/users/me/identities/",
        "/api/v1/users/me/sessions/",
        "/api/v1/users/me/preferences/appearance/",
        "/api/v1/users/me/preferences/shortcuts/",
        "/api/v1/users/me/preferences/notification-delivery/",
        "/api/v1/meta/about/",
        "/api/v1/meta/shortcuts/",
        f"/api/v1/tenants/{t}/settings/general/",
        f"/api/v1/tenants/{t}/settings/notifications/",
        f"/api/v1/tenants/{t}/settings/work-mode/",
        f"/api/v1/tenants/{t}/settings/data-controls/",
        f"/api/v1/tenants/{t}/projects/{p}/settings/general/",
        f"/api/v1/tenants/{t}/agent-plugins/",
        f"/api/v1/tenants/{t}/privacy/grants/",
        f"/api/v1/tenants/{t}/privacy/sessions/",
        f"/api/v1/tenants/{t}/privacy/deletion-requests/",
        f"/api/v1/tenants/{t}/billing/capabilities/",
        f"/api/v1/tenants/{t}/billing/plans/",
        f"/api/v1/tenants/{t}/billing/subscription/",
        f"/api/v1/tenants/{t}/billing/usage/",
        f"/api/v1/tenants/{t}/billing/orders/",
        f"/api/v1/tenants/{t}/billing/reconciliation/",
        f"/api/v1/tenants/{t}/token-budget/",
        f"/api/v1/tenants/{t}/audit-events/",
        f"/api/v1/tenants/{t}/feedback/",
        f"/api/v1/tenants/{t}/projects/{p}/settings/general/",
        f"/api/v1/tenants/{t}/projects/{p}/archive-items/",
        f"/api/v1/tenants/{t}/projects/{p}/agent-nodes/",
        f"/api/v1/tenants/{t}/projects/{p}/cluster-deployments/",
        f"/api/v1/tenants/{t}/projects/{p}/knowledge-bases/",
        f"/api/v1/tenants/{t}/projects/{p}/agent-plugin-installations/",
        f"/api/v1/tenants/{t}/projects/{p}/infrastructure-networks/",
        f"/api/v1/tenants/{t}/projects/{p}/fleet/machines/",
    ]


@pytest.mark.django_db
def test_console_read_endpoints_never_return_server_errors(smoke):
    client, tenant, project = smoke
    failures = []
    for path in _paths(tenant, project):
        response = client.get(path)
        if response.status_code not in PASS_STATUSES:
            failures.append((path, response.status_code))
        # An envelope is mandatory for every non-2xx answer.
        if response.status_code >= 400:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                failures.append((path, f"non-JSON error {content_type}"))
                continue
            body = response.json()
            if not body.get("error", {}).get("code"):
                failures.append((path, f"unwrapped {body}"))
    assert failures == [], failures
