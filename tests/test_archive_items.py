import pytest
from core.models import (
    AgentDefinition,
    AgentTeam,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def archive_workspace(db):
    user = get_user_model().objects.create_user(
        username="dc-archive-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="DC Archive Tenant", slug="dc-archive-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project_a = Project.objects.create(
        tenant=tenant, name="Archive Project A", slug="archive-project-a"
    )
    project_b = Project.objects.create(
        tenant=tenant, name="Archive Project B", slug="archive-project-b"
    )
    ProjectMembership.objects.create(
        project=project_a, user=user, role=ProjectMembership.Role.ADMIN
    )
    ProjectMembership.objects.create(
        project=project_b, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {
        "user": user,
        "tenant": tenant,
        "project_a": project_a,
        "project_b": project_b,
        "client": client,
    }


def _archived_agent(workspace, project, slug, name):
    return AgentDefinition.objects.create(
        tenant=workspace["tenant"],
        project=project,
        created_by=workspace["user"],
        name=name,
        slug=slug,
        description="Archived agent definition.",
        is_archived=True,
    )


@pytest.mark.django_db
def test_archive_list_scoped_to_project(archive_workspace):
    agent_a1 = _archived_agent(archive_workspace, archive_workspace["project_a"], "agent-a1", "Alpha Agent")
    agent_a2 = _archived_agent(archive_workspace, archive_workspace["project_a"], "agent-a2", "Beta Agent")
    _archived_agent(archive_workspace, archive_workspace["project_b"], "agent-b1", "Gamma Agent")

    list_url = (
        f"/api/v1/tenants/{archive_workspace['tenant'].id}"
        f"/projects/{archive_workspace['project_a'].id}/archive-items/"
    )
    response = archive_workspace["client"].get(list_url)
    assert response.status_code == 200
    items = response.data["results"]
    assert {item["item_id"] for item in items} == {str(agent_a1.id), str(agent_a2.id)}
    assert all(item["project_id"] == str(archive_workspace["project_a"].id) for item in items)

    typed = archive_workspace["client"].get(list_url, {"type": "agent"})
    assert typed.status_code == 200
    assert all(item["item_type"] == "agent" for item in typed.data["results"])

    searched = archive_workspace["client"].get(list_url, {"q": "Alpha"})
    assert searched.status_code == 200
    assert [item["name"] for item in searched.data["results"]] == ["Alpha Agent"]


@pytest.mark.django_db
def test_archive_restore_idempotent(archive_workspace):
    agent = _archived_agent(archive_workspace, archive_workspace["project_a"], "restore-me", "Restore Me")
    url = (
        f"/api/v1/tenants/{archive_workspace['tenant'].id}"
        f"/projects/{archive_workspace['project_a'].id}"
        f"/archive-items/agent/{agent.id}/restore/"
    )

    first = archive_workspace["client"].post(url, {}, format="json")
    assert first.status_code == 200
    agent.refresh_from_db()
    assert agent.is_archived is False

    second = archive_workspace["client"].post(url, {}, format="json")
    assert second.status_code == 200
    assert second.data["already_restored"] is True

    from core.data_controls_models import ArchiveAction

    actions = ArchiveAction.objects.filter(
        tenant=archive_workspace["tenant"],
        item_type="agent",
        item_id=str(agent.id),
        action="restore",
    )
    assert actions.count() == 1


@pytest.mark.django_db
def test_archive_purge_requires_archived_and_scoped(archive_workspace):
    active_agent = AgentDefinition.objects.create(
        tenant=archive_workspace["tenant"],
        project=archive_workspace["project_a"],
        created_by=archive_workspace["user"],
        name="Active Agent",
        slug="active-agent",
        is_archived=False,
    )
    foreign_archived = _archived_agent(
        archive_workspace, archive_workspace["project_b"], "foreign-archived", "Foreign Archived"
    )
    own_archived = _archived_agent(
        archive_workspace, archive_workspace["project_a"], "own-archived", "Own Archived"
    )

    base = (
        f"/api/v1/tenants/{archive_workspace['tenant'].id}"
        f"/projects/{archive_workspace['project_a'].id}/archive-items"
    )

    active_response = archive_workspace["client"].delete(f"{base}/agent/{active_agent.id}/")
    assert active_response.status_code == 404

    foreign_response = archive_workspace["client"].delete(f"{base}/agent/{foreign_archived.id}/")
    assert foreign_response.status_code == 404

    purge_response = archive_workspace["client"].delete(f"{base}/agent/{own_archived.id}/")
    assert purge_response.status_code == 204
    assert not AgentDefinition.objects.filter(id=own_archived.id).exists()

    from core.data_controls_models import ArchiveAction

    purge_actions = ArchiveAction.objects.filter(
        tenant=archive_workspace["tenant"],
        item_type="agent",
        item_id=str(own_archived.id),
        action="purge",
    )
    assert purge_actions.count() == 1

    unknown_type = archive_workspace["client"].get(
        f"/api/v1/tenants/{archive_workspace['tenant'].id}"
        f"/projects/{archive_workspace['project_a'].id}/archive-items/?type=widget"
    )
    assert unknown_type.status_code == 400
    assert unknown_type.data["error"]["code"] == "ARCHIVE_ITEM_TYPE_UNSUPPORTED"
