import pytest
from core.models import (
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
)
from core.workflow_engine import WorkflowValidationError, validate_workflow_definition
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from worker_app.tasks import process_once


def linear_workflow(model_id):
    return {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "answer",
                "type": "llm",
                "config": {
                    "model_config_id": str(model_id),
                    "prompt_template": "Answer this: {{input}}",
                    "max_tokens": 256,
                    "timeout_seconds": 20,
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "answer"}},
        ],
        "edges": [
            {"source": "start", "target": "answer"},
            {"source": "answer", "target": "end"},
        ],
    }


@pytest.fixture
def workflow_workspace(db):
    user = get_user_model().objects.create_user(
        username="workflow-editor",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Workflow tenant", slug="workflow-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(
        tenant=tenant,
        name="Workflow project",
        slug="workflow-project",
    )
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Workflow fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "workflow response"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="workflow-fake",
        display_name="Workflow Fake",
        capabilities=["chat"],
        max_output_tokens=512,
    )
    return user, tenant, project, model


def test_workflow_validator_rejects_cycles_and_disconnected_nodes():
    cyclic = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "end"},
            {"source": "end", "target": "start"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="cycle"):
        validate_workflow_definition(cyclic)

    disconnected = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {}},
            {
                "id": "orphan",
                "type": "llm",
                "config": {"model_config_id": "x", "prompt_template": "x"},
            },
        ],
        "edges": [{"source": "start", "target": "end"}],
    }
    with pytest.raises(WorkflowValidationError, match="disconnected"):
        validate_workflow_definition(disconnected)


@pytest.mark.django_db
def test_workflow_draft_publish_and_persistent_run(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/workflows/",
        {
            "name": "Simple workflow",
            "slug": "simple-workflow",
            "description": "Start, LLM and end",
            "draft_definition": linear_workflow(model.id),
        },
        format="json",
    )
    assert created.status_code == 201
    workflow_id = created.data["id"]

    published = client.post(
        f"/api/v1/workflows/{workflow_id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201
    assert published.data["version"] == 1

    run_response = client.post(
        f"/api/v1/workflows/{workflow_id}/runs/",
        {"input": {"question": "What is a workflow?"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-run-001",
    )
    assert run_response.status_code == 202
    run_id = run_response.data["id"]
    assert run_response.data["task_status"] == Task.Status.QUEUED

    assert process_once("workflow-test-worker") is True

    run = WorkflowRun.objects.select_related("task").get(pk=run_id)
    assert run.task.status == Task.Status.SUCCEEDED
    assert run.output_data["content"] == "workflow response"
    assert WorkflowStepRun.objects.filter(
        workflow_run=run,
        status=WorkflowStepRun.Status.SUCCEEDED,
    ).count() == 3


@pytest.mark.django_db
def test_workflow_publish_rejects_unapproved_node_types(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    definition = linear_workflow(model.id)
    definition["nodes"].insert(
        1,
        {"id": "code", "type": "code", "config": {"source": "unsafe"}},
    )
    definition["edges"] = [
        {"source": "start", "target": "code"},
        {"source": "code", "target": "answer"},
        {"source": "answer", "target": "end"},
    ]
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Unsupported node",
        slug="unsupported-node",
        draft_definition=definition,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/",
        {},
        format="json",
    )

    assert response.status_code == 400
    assert workflow.versions.count() == 0


@pytest.mark.django_db
def test_workflow_run_requires_published_version(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Draft workflow",
        slug="draft-workflow",
        draft_definition=linear_workflow(model.id),
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-draft-run",
    )

    assert response.status_code == 409
    assert Task.objects.count() == 0
