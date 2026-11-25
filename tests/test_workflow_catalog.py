"""The studio needs the identifiers a node can reference, scoped like validation."""

import pytest
from core.knowledge_models import KnowledgeBase
from core.models import (
    AgentDefinition,
    AgentPluginDefinition,
    AgentPluginInstallation,
    AgentVersion,
    ModelConfig,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def catalog_workspace(db):
    user = get_user_model().objects.create_user(username="catalog", password="catalog-pass")
    tenant = Tenant.objects.create(name="Catalog tenant", slug="catalog-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Catalog project", slug="catalog-proj")
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    other_project = Project.objects.create(
        tenant=tenant, name="Other project", slug="catalog-other"
    )
    from core.models import ModelProvider

    provider_row = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Catalog fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={},
    )
    model = ModelConfig.objects.create(
        provider=provider_row,
        model_name="catalog-fake",
        display_name="Catalog Fake",
        capabilities=["chat"],
    )
    base = KnowledgeBase.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        embedding_model=model,
        name="Runbooks",
        slug="runbooks",
    )
    plugin = AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="catalog-plugin",
        name="Catalog Connector",
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
        endpoint_url="",
        auth_required=False,
        capabilities=["lookup"],
        manifest={
            "capabilities": [
                {
                    "name": "lookup",
                    "description": "Look up a record",
                    "risk_level": "low",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]
        },
    )
    AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=AgentPluginInstallation.Status.INSTALLED,
        granted_capabilities=["lookup"],
        installed_by=user,
    )
    agent = AgentDefinition.objects.create(
        tenant=tenant, project=project, created_by=user, name="Ops Agent", slug="ops-agent"
    )
    version = AgentVersion.objects.create(
        agent=agent, version=1, config_snapshot={"model_config_id": str(model.id)}, created_by=user
    )
    return user, tenant, project, other_project, model, base, "lookup", version


@pytest.mark.django_db
def test_catalog_lists_references_the_studio_requires(catalog_workspace):
    user, tenant, project, _, model, base, tool, version = catalog_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/{project.id}/workflow-catalog/")
    assert response.status_code == 200
    body = response.data
    assert [item["id"] for item in body["models"]] == [str(model.id)]
    assert body["models"][0]["label"] == "Catalog Fake"
    assert [item["id"] for item in body["knowledge_bases"]] == [str(base.id)]
    assert [item["id"] for item in body["tools"]] == ["lookup"]
    assert body["tools"][0]["risk_level"] == "low"
    assert [item["id"] for item in body["agents"]] == [str(version.id)]
    assert body["agents"][0]["version"] == 1


@pytest.mark.django_db
def test_catalog_hides_references_outside_the_requested_project(catalog_workspace):
    """A tool installed in another project is not usable, so it must not be offered."""

    user, tenant, project, other_project, *_ = catalog_workspace
    archive = KnowledgeBase.objects.create(
        tenant=tenant,
        project=other_project,
        created_by=user,
        embedding_model=ModelConfig.objects.first(),
        name="Other runbooks",
        slug="other-runbooks",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/{project.id}/workflow-catalog/")
    assert response.status_code == 200
    ids = {item["id"] for item in response.data["knowledge_bases"]}
    assert str(archive.id) not in ids


@pytest.mark.django_db
def test_catalog_requires_tenant_membership(db):
    user = get_user_model().objects.create_user(username="outsider", password="outsider-pass")
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(
        "/api/v1/tenants/00000000-0000-0000-0000-000000000000/projects/x/workflow-catalog/"
    )
    assert response.status_code in (403, 404)


@pytest.mark.django_db
def test_per_branch_insertion_is_valid_while_a_shared_join_is_not(catalog_workspace):
    """A condition cannot send both guarded branches to one node.

    The studio therefore inserts one node per branch, which is what the studio
    writes; a single join node would produce a duplicate edge and be rejected.
    """

    from core.workflow_engine import WorkflowValidationError, validate_workflow_definition

    _, _, _, _, model, *_ = catalog_workspace
    per_branch = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "check", "type": "condition", "config": {"input_key": "input", "equals": "yes"}},
            {
                "id": "llm_true",
                "type": "llm",
                "config": {"model_config_id": str(model.id), "prompt_template": "{{input}}"},
            },
            {
                "id": "llm_false",
                "type": "llm",
                "config": {"model_config_id": str(model.id), "prompt_template": "{{input}}"},
            },
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "check"},
            {"source": "check", "target": "llm_true", "when": True},
            {"source": "check", "target": "llm_false", "when": False},
            {"source": "llm_true", "target": "end"},
            {"source": "llm_false", "target": "end"},
        ],
    }
    graph = validate_workflow_definition(per_branch)
    assert set(graph["nodes"]) == {"start", "check", "llm_true", "llm_false", "end"}

    shared_join = {
        "nodes": per_branch["nodes"][:2]
        + [
            {
                "id": "join",
                "type": "llm",
                "config": {
                    "model_config_id": str(model.id),
                    "prompt_template": "{{input}}",
                    "join": "any",
                },
            },
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "check"},
            {"source": "check", "target": "join", "when": True},
            {"source": "check", "target": "join", "when": False},
            {"source": "join", "target": "end"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="duplicate edges"):
        validate_workflow_definition(shared_join)
