from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.models import (
    KnowledgeBase,
    ModelConfig,
    ModelProvider,
    PluginDefinition,
    PluginInstallation,
    PluginTool,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
)
from core.workflow_engine import (
    WorkflowValidationError,
    _execute_node,
    validate_model_references,
    validate_workflow_definition,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from worker_app.tasks import process_once, renew_lease


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
            {"id": "cycle_a", "type": "condition", "config": {"equals": True}},
            {"id": "cycle_b", "type": "condition", "config": {"equals": False}},
        ],
        "edges": [
            {"source": "start", "target": "end"},
            {"source": "cycle_a", "target": "cycle_b", "when": True},
            {"source": "cycle_b", "target": "cycle_a", "when": False},
            {"source": "cycle_b", "target": "end", "when": True},
            {"source": "cycle_a", "target": "end", "when": False},
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
                "config": {
                    "model_config_id": "00000000-0000-0000-0000-000000000001",
                    "prompt_template": "x",
                },
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

    validation = client.post(
        f"/api/v1/workflows/{workflow_id}/validate/",
        {},
        format="json",
    )
    assert validation.status_code == 200
    assert validation.data["is_valid"] is True
    assert validation.data["execution_layers"] == [["start"], ["answer"], ["end"]]

    workflow_record = WorkflowDefinition.objects.get(pk=workflow_id)
    valid_definition = workflow_record.draft_definition
    workflow_record.draft_definition = {"nodes": [], "edges": []}
    workflow_record.save(update_fields=["draft_definition", "updated_at"])
    invalid_validation = client.post(
        f"/api/v1/workflows/{workflow_id}/validate/",
        {},
        format="json",
    )
    assert invalid_validation.status_code == 200
    assert invalid_validation.data["is_valid"] is False
    workflow_record.draft_definition = valid_definition
    workflow_record.save(update_fields=["draft_definition", "updated_at"])

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

    with patch("worker_app.tasks.renew_lease", wraps=renew_lease) as heartbeat:
        assert process_once("workflow-test-worker") is True

    assert heartbeat.call_count == 6
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


@pytest.mark.django_db
def test_workflow_condition_executes_only_matching_branch(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "check",
                "type": "condition",
                "config": {"input_key": "mode", "equals": "fast"},
            },
            {
                "id": "fast_answer",
                "type": "llm",
                "config": {
                    "model_config_id": str(model.id),
                    "prompt_template": "Fast: {{input}}",
                    "max_tokens": 128,
                    "timeout_seconds": 20,
                },
            },
            {
                "id": "slow_answer",
                "type": "llm",
                "config": {
                    "model_config_id": str(model.id),
                    "prompt_template": "Slow: {{input}}",
                    "max_tokens": 128,
                    "timeout_seconds": 20,
                },
            },
            {
                "id": "end",
                "type": "end",
                "config": {"source_node": "fast_answer"},
            },
        ],
        "edges": [
            {"source": "start", "target": "check"},
            {"source": "check", "target": "fast_answer", "when": True},
            {"source": "check", "target": "slow_answer", "when": False},
            {"source": "fast_answer", "target": "end"},
            {"source": "slow_answer", "target": "end"},
        ],
    }
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Conditional workflow",
        slug="conditional-workflow",
        draft_definition=definition,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    published = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201
    run_response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {"mode": "fast", "question": "choose a branch"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-branch-001",
    )
    assert run_response.status_code == 202

    assert process_once("workflow-branch-worker") is True

    run = WorkflowRun.objects.get(pk=run_response.data["id"])
    assert run.task.status == Task.Status.SUCCEEDED
    assert run.output_data["content"] == "workflow response"
    assert WorkflowStepRun.objects.get(
        workflow_run=run,
        node_id="slow_answer",
    ).status == WorkflowStepRun.Status.SKIPPED


def test_workflow_validator_rejects_embedded_credentials():
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "end",
                "type": "end",
                "config": {"headers": {"Authorization": "Bearer do-not-store"}},
            },
        ],
        "edges": [{"source": "start", "target": "end"}],
    }
    with pytest.raises(WorkflowValidationError, match="credential material"):
        validate_workflow_definition(definition)


@pytest.mark.django_db
def test_workflow_detail_is_not_visible_across_tenants(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    other_user = get_user_model().objects.create_user(
        username="other-tenant-user",
        password="test-password",
    )
    other_tenant = Tenant.objects.create(
        name="Other tenant",
        slug="other-tenant",
    )
    TenantMembership.objects.create(
        tenant=other_tenant,
        user=other_user,
        role="member",
    )
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Private workflow",
        slug="private-workflow",
        draft_definition=linear_workflow(model.id),
    )
    client = APIClient()
    client.force_authenticate(user=other_user)

    response = client.get(f"/api/v1/workflows/{workflow.id}/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_workflow_viewer_cannot_edit_or_publish(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    viewer = get_user_model().objects.create_user(
        username="workflow-viewer",
        password="test-password",
    )
    TenantMembership.objects.create(tenant=tenant, user=viewer, role="member")
    ProjectMembership.objects.create(project=project, user=viewer, role="viewer")
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Viewer protected workflow",
        slug="viewer-protected",
        draft_definition=linear_workflow(model.id),
    )
    client = APIClient()
    client.force_authenticate(user=viewer)

    patch_response = client.patch(
        f"/api/v1/workflows/{workflow.id}/",
        {"description": "unauthorized edit"},
        format="json",
    )
    publish_response = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/",
        {},
        format="json",
    )

    assert patch_response.status_code == 403
    assert publish_response.status_code == 403
    assert workflow.versions.count() == 0


def test_workflow_validator_requires_uuid_model_reference():
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "answer",
                "type": "llm",
                "config": {
                    "model_config_id": "not-a-uuid",
                    "prompt_template": "Answer {{input}}",
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "answer"}},
        ],
        "edges": [
            {"source": "start", "target": "answer"},
            {"source": "answer", "target": "end"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="must be a UUID"):
        validate_workflow_definition(definition)


def test_workflow_validator_rejects_invalid_condition_key_and_future_source():
    invalid_condition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "check",
                "type": "condition",
                "config": {"input_key": ["not", "a", "key"], "equals": True},
            },
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "check"},
            {"source": "check", "target": "end", "when": True},
            {"source": "check", "target": "end", "when": False},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="input_key"):
        validate_workflow_definition(invalid_condition)

    future_source = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "answer",
                "type": "llm",
                "config": {
                    "model_config_id": "00000000-0000-0000-0000-000000000001",
                    "prompt_template": "{{input}}",
                    "input_from": "end",
                },
            },
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "answer"},
            {"source": "answer", "target": "end"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="earlier node"):
        validate_workflow_definition(future_source)


@pytest.mark.django_db
def test_workflow_run_idempotency_and_request_shape(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Idempotent workflow",
        slug="idempotent-workflow",
        draft_definition=linear_workflow(model.id),
    )
    client = APIClient()
    client.force_authenticate(user=user)
    published = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201

    first = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {"message": "same request"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-idempotent-001",
    )
    repeated = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {"message": "same request"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-idempotent-001",
    )
    conflict = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {"message": "different request"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-idempotent-001",
    )
    invalid_shape = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        [{"message": "not an object"}],
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-idempotent-002",
    )

    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.data["id"] == first.data["id"]
    assert conflict.status_code == 409
    assert invalid_shape.status_code == 400
    assert Task.objects.filter(task_type="workflow.run").count() == 1



@pytest.mark.django_db
def test_workflow_knowledge_search_node_retrieves_pinned_base(workflow_workspace):
    user, tenant, project, chat_model = workflow_workspace
    embedding_provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Workflow embedding provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    embedding_model = ModelConfig.objects.create(
        provider=embedding_provider,
        model_name="workflow-embedding",
        display_name="Workflow embedding",
        capabilities=["embeddings"],
    )
    knowledge_base = KnowledgeBase.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        embedding_model=embedding_model,
        name="Workflow runbooks",
        slug="workflow-runbooks",
    )
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "search",
                "type": "knowledge_search",
                "config": {
                    "knowledge_base_ids": [str(knowledge_base.id)],
                    "query_template": "{{input}}",
                    "limit": 3,
                },
            },
            {
                "id": "answer",
                "type": "llm",
                "config": {
                    "model_config_id": str(chat_model.id),
                    "prompt_template": "Answer using these references: {{input}}",
                    "input_from": "search",
                    "max_tokens": 256,
                    "timeout_seconds": 20,
                },
            },
            {
                "id": "end",
                "type": "end",
                "config": {"source_node": "answer"},
            },
        ],
        "edges": [
            {"source": "start", "target": "search"},
            {"source": "search", "target": "answer"},
            {"source": "answer", "target": "end"},
        ],
    }
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Knowledge workflow",
        slug="knowledge-workflow",
        draft_definition=definition,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    published = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/",
        {},
        format="json",
    )
    assert published.status_code == 201
    run_response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {"question": "How do I check cluster health?"}},
        format="json",
        HTTP_IDEMPOTENCY_KEY="workflow-knowledge-run",
    )
    assert run_response.status_code == 202

    class FakeGateway:
        def __init__(self):
            self.messages = []

        def complete(self, model_config, messages, **kwargs):
            self.messages = list(messages)
            return SimpleNamespace(
                content="Check node readiness and API health.",
                input_tokens=12,
                output_tokens=8,
                latency_ms=5,
                provider="fake",
                model="workflow-fake",
                tool_calls=[],
            )

    gateway = FakeGateway()
    with patch(
        "core.workflow_engine.retrieve_context",
        return_value=[
            {
                "text": "Check that all nodes are Ready.",
                "score": 0.92,
                "document_id": "doc-1",
                "chunk_id": "doc-1:0",
            }
        ],
    ) as retrieval:
        with patch("core.workflow_engine.ModelGateway", return_value=gateway):
            assert process_once("workflow-knowledge-worker") is True

    run = WorkflowRun.objects.select_related("task").get(pk=run_response.data["id"])
    search_step = WorkflowStepRun.objects.get(workflow_run=run, node_id="search")
    assert run.task.status == Task.Status.SUCCEEDED
    assert run.output_data["content"] == "Check node readiness and API health."
    assert search_step.output_data["results"][0]["chunk_id"] == "doc-1:0"
    assert "UNTRUSTED RETRIEVED REFERENCE DATA" in gateway.messages[1]["content"]
    assert retrieval.call_args.kwargs["metadata_filters"] == {
        "knowledge_base_id": str(knowledge_base.id)
    }



@pytest.mark.django_db
def test_workflow_publish_rejects_knowledge_base_from_another_project(workflow_workspace):
    user, tenant, project, chat_model = workflow_workspace
    embedding_provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Other project embedding provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    embedding_model = ModelConfig.objects.create(
        provider=embedding_provider,
        model_name="other-project-embedding",
        display_name="Other project embedding",
        capabilities=["embeddings"],
    )
    other_project = Project.objects.create(
        tenant=tenant,
        name="Other knowledge project",
        slug="other-knowledge-project",
    )
    other_base = KnowledgeBase.objects.create(
        tenant=tenant,
        project=other_project,
        created_by=user,
        embedding_model=embedding_model,
        name="Private knowledge",
        slug="private-knowledge",
    )
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "search",
                "type": "knowledge_search",
                "config": {"knowledge_base_ids": [str(other_base.id)]},
            },
            {"id": "end", "type": "end", "config": {"source_node": "search"}},
        ],
        "edges": [
            {"source": "start", "target": "search"},
            {"source": "search", "target": "end"},
        ],
    }
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Cross project workflow",
        slug="cross-project-workflow",
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


def test_workflow_validator_accepts_tool_node_with_project_tool_reference():
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "tool_step",
                "type": "tool",
                "config": {
                    "tool_id": "00000000-0000-0000-0000-000000000001",
                    "arguments": {"query": "hello"},
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "tool_step"}},
        ],
        "edges": [
            {"source": "start", "target": "tool_step"},
            {"source": "tool_step", "target": "end"},
        ],
    }
    graph = validate_workflow_definition(definition)
    assert graph["order"] == ["start", "tool_step", "end"]


@pytest.mark.parametrize(
    "tool_config, message",
    [
        ({}, "requires tool_id"),
        ({"tool_id": "not-a-uuid"}, "must be a UUID"),
        (
            {
                "tool_id": "00000000-0000-0000-0000-000000000001",
                "arguments": [],
            },
            "arguments must be an object",
        ),
    ],
)
def test_workflow_validator_rejects_malformed_tool_node(tool_config, message):
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "tool_step", "type": "tool", "config": tool_config},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "tool_step"},
            {"source": "tool_step", "target": "end"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match=message):
        validate_workflow_definition(definition)


@pytest.mark.django_db
def test_workflow_tool_node_requires_approved_project_installation(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    plugin = PluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="workflow-test",
        name="Workflow Test Connector",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.HTTP,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
    )
    tool = PluginTool.objects.create(
        plugin=plugin,
        name="search",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        risk_level=PluginTool.RiskLevel.LOW,
        idempotency_mode=PluginTool.IdempotencyMode.READ_ONLY,
    )
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "tool_step",
                "type": "tool",
                "config": {
                    "tool_id": str(tool.id),
                    "arguments": {"query": "hello"},
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "tool_step"}},
        ],
        "edges": [
            {"source": "start", "target": "tool_step"},
            {"source": "tool_step", "target": "end"},
        ],
    }
    graph = validate_workflow_definition(definition)
    with pytest.raises(WorkflowValidationError, match="not installed and approved"):
        validate_model_references(graph, tenant, project)

    PluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=PluginInstallation.Status.INSTALLED,
        granted_permissions=[],
        installed_by=user,
    )
    validate_model_references(graph, tenant, project)

    tool.risk_level = PluginTool.RiskLevel.HIGH
    tool.save(update_fields=["risk_level", "updated_at"])
    with pytest.raises(WorkflowValidationError, match="human approval"):
        validate_model_references(graph, tenant, project)


@pytest.mark.django_db
def test_workflow_tool_node_executes_through_policy_router(workflow_workspace):
    user, tenant, project, model = workflow_workspace
    plugin = PluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="workflow-runtime",
        name="Workflow Runtime Connector",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.HTTP,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
    )
    tool = PluginTool.objects.create(
        plugin=plugin,
        name="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 100}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        risk_level=PluginTool.RiskLevel.LOW,
        idempotency_mode=PluginTool.IdempotencyMode.READ_ONLY,
    )
    installation = PluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=PluginInstallation.Status.INSTALLED,
        granted_permissions=[],
        installed_by=user,
    )
    task = SimpleNamespace(id="workflow-task", created_by=user)

    def register_handler(router, registered_tool):
        router.register(
            plugin.plugin_key,
            registered_tool.name,
            lambda arguments, context: {"ok": arguments["query"] == "hello"},
            plugin_version=plugin.version,
        )

    with patch("core.workflow_engine.register_http_tool", side_effect=register_handler):
        with patch("core.workflow_engine.record_audit_event") as audit:
            result = _execute_node(
                {
                    "id": "tool_step",
                    "type": "tool",
                    "config": {"tool_id": str(tool.id), "arguments": {"query": "hello"}},
                },
                {"input": {}},
                {},
                tenant,
                project,
                task,
            )

    assert result == {"ok": True}
    assert audit.call_count == 1
    assert (
        PluginInstallation.objects.get(pk=installation.id).status
        == PluginInstallation.Status.INSTALLED
    )


def test_workflow_tool_arguments_must_reference_an_earlier_node():
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "tool_step",
                "type": "tool",
                "config": {
                    "tool_id": "00000000-0000-0000-0000-000000000001",
                    "arguments_from": "later",
                },
            },
            {"id": "later", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "tool_step"},
            {"source": "tool_step", "target": "later"},
        ],
    }
    with pytest.raises(WorkflowValidationError, match="earlier node"):
        validate_workflow_definition(definition)
