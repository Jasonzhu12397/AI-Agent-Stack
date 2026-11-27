"""Write verbs tunnelled through POST must reach the view unchanged."""

import pytest
from core.chat_models import AgentChatThread
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.test import Client


@pytest.mark.django_db
def test_post_with_override_header_is_treated_as_delete():
    user = get_user_model().objects.create_user(username="override-user", password="pw-12345678")
    tenant = Tenant.objects.create(name="Override tenant", slug="override-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(
        tenant=tenant, name="Override project", slug="override-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.VIEWER
    )
    thread = AgentChatThread.objects.create(
        tenant=tenant, project=project, created_by=user, title="Temporary"
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    # Prime the CSRF cookie the same way the console does.
    client.get("/api/v1/auth/csrf/")
    token = client.cookies["csrftoken"].value
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread.id}/",
        HTTP_X_HTTP_METHOD_OVERRIDE="DELETE",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 204
    assert not AgentChatThread.objects.filter(pk=thread.id).exists()


@pytest.mark.django_db
def test_plain_post_without_override_still_creates_a_message():
    user = get_user_model().objects.create_user(username="override-noop", password="pw-12345678")
    tenant = Tenant.objects.create(name="Plain tenant", slug="plain-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Plain project", slug="plain-project")
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.VIEWER
    )
    client = Client()
    client.force_login(user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/threads/",
        {"title": "Plain"},
        content_type="application/json",
    )
    assert response.status_code == 201
    assert AgentChatThread.objects.filter(tenant=tenant, title="Plain").exists()
