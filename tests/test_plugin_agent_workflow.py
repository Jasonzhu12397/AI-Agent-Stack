import json
from unittest.mock import patch

import pytest
from core.agent_capability_router import (
    CAPABILITY_DISABLED,
    CAPABILITY_NOT_INSTALLED,
)
from core.models import (
    AgentCapabilityInvocation,
    AgentPluginDefinition,
    AgentPluginInstallation,
    AgentVersion,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
)
from core.plugin_agent_binding import (
    BINDING_VERSION_MISMATCH,
    bind_capability,
)
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path
from rest_framework.test import APIClient
from worker_app.tasks import process_once

SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _manifest(capability="summarize"):
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {
                capability: {
                    "method": "GET",
                    "path": "/v1/" + capability,
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": capability,
                "description": "Summarize input text",
                "input_schema": TEXT_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


# Test-local URLconf: includes the existing platform routes plus the binding
# routes, mirroring the merge-time include in core/urls.py.
urlpatterns = [
    path("", include("core.urls")),
    path("", include("core.plugin_agent_urls")),
]


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._payload


class FakeClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.captured.update({"method": method, "url": url, **kwargs})
        return self.response


def _mock_http(monkeypatch, captured, payload):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    return patch(
        "core.http_connector.httpx.Client",
        return_value=FakeClient(FakeResponse(payload), captured),
    )


@pytest.fixture
def workflow_binding_workspace(db):
    user = get_user_model().objects.create_user(
        username="wf-binding-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Workflow binding tenant", slug="wf-binding-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Workflow binding project", slug="wf-binding-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, version="1.0.0", key="market-agent-a"):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key,
        name="Market Agent",
        version=version,
        protocol="http_json",
        endpoint_url="https://market-agent.example.com/v1/invoke",
        auth_required=False,
        capabilities=["summarize"],
        manifest=_manifest(),
    )


def _install(tenant, project, plugin, user, status="installed"):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=status,
        granted_capabilities=["summarize"],
        installed_by=user,
    )


def _workflow_definition(agent_version_id, *, capability="summarize", arguments=None):
    return {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "summarize",
                "type": "agent",
                "config": {
                    "agent_version_id": str(agent_version_id),
                    "capability": capability,
                    "arguments": arguments or {"text": "hello"},
                },
            },
            {"id": "end", "type": "end", "config": {"source_node": "summarize"}},
        ],
        "edges": [
            {"source": "start", "target": "summarize"},
            {"source": "summarize", "target": "end"},
        ],
    }


def _publish(client, tenant, project, user, definition, slug):
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Marketplace workflow",
        slug=slug,
        draft_definition=definition,
    )
    published = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/", {}, format="json"
    )
    assert published.status_code == 201
    return workflow


def _start_run(client, workflow, key):
    response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs/",
        {"input": {}},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert response.status_code == 202
    return response.data["id"]


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_workflow")
def test_workflow_executes_bound_marketplace_agent(
    workflow_binding_workspace, monkeypatch
):
    user, tenant, project, client = workflow_binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    bound = bind_capability(installation, "summarize", user)

    workflow = _publish(
        client,
        tenant,
        project,
        user,
        _workflow_definition(bound.version.id),
        "marketplace-success",
    )
    run_id = _start_run(client, workflow, "wf-marketplace-success-001")

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "done"}):
            assert process_once("wf-marketplace-worker") is True

    run = WorkflowRun.objects.select_related("task").get(pk=run_id)
    assert run.task.status == Task.Status.SUCCEEDED
    step = WorkflowStepRun.objects.get(workflow_run=run, node_id="summarize")
    assert step.status == WorkflowStepRun.Status.SUCCEEDED
    assert step.output_data["summary"] == "done"
    assert step.output_data["capability"] == "summarize"
    assert step.output_data["plugin_key"] == "market-agent-a"
    assert step.output_data["agent_version"] == bound.version.version
    assert run.output_data["summary"] == "done"

    record = AgentCapabilityInvocation.objects.get(
        installation=installation, capability="summarize"
    )
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert record.protocol == "http_json"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://market-agent.example.com/v1/summarize"


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_workflow")
def test_workflow_fails_explicitly_after_uninstall(
    workflow_binding_workspace, monkeypatch
):
    user, tenant, project, client = workflow_binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    bound = bind_capability(installation, "summarize", user)
    workflow = _publish(
        client,
        tenant,
        project,
        user,
        _workflow_definition(bound.version.id),
        "marketplace-e2e",
    )

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "first run"}):
            first_run_id = _start_run(client, workflow, "wf-marketplace-e2e-001")
            assert process_once("wf-marketplace-worker") is True

    first_run = WorkflowRun.objects.select_related("task").get(pk=first_run_id)
    assert first_run.task.status == Task.Status.SUCCEEDED

    # Uninstall: the same workflow must fail on the next run, never succeed
    # silently or fabricate an empty result.
    installation.status = AgentPluginInstallation.Status.UNINSTALLED
    installation.save(update_fields=["status", "updated_at"])

    second_run_id = _start_run(client, workflow, "wf-marketplace-e2e-002")
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "should not be called"}):
            assert process_once("wf-marketplace-worker") is True

    second_run = WorkflowRun.objects.select_related("task").get(pk=second_run_id)
    assert second_run.task.status == Task.Status.FAILED
    step = WorkflowStepRun.objects.get(workflow_run=second_run, node_id="summarize")
    assert step.status == WorkflowStepRun.Status.FAILED
    assert step.error["code"] == CAPABILITY_NOT_INSTALLED
    assert step.output_data is None

    # The uninstalled installation must not produce a second success record.
    assert (
        AgentCapabilityInvocation.objects.filter(
            installation=installation,
            status=AgentCapabilityInvocation.Status.SUCCESS,
        ).count()
        == 1
    )


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_workflow")
def test_workflow_fails_when_installation_disabled(
    workflow_binding_workspace, monkeypatch
):
    user, tenant, project, client = workflow_binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    bound = bind_capability(installation, "summarize", user)
    workflow = _publish(
        client,
        tenant,
        project,
        user,
        _workflow_definition(bound.version.id),
        "marketplace-disabled",
    )

    installation.status = AgentPluginInstallation.Status.DISABLED
    installation.save(update_fields=["status", "updated_at"])

    run_id = _start_run(client, workflow, "wf-marketplace-disabled-001")
    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "should not be called"}):
            assert process_once("wf-marketplace-worker") is True

    run = WorkflowRun.objects.select_related("task").get(pk=run_id)
    assert run.task.status == Task.Status.FAILED
    step = WorkflowStepRun.objects.get(workflow_run=run, node_id="summarize")
    assert step.status == WorkflowStepRun.Status.FAILED
    assert step.error["code"] == CAPABILITY_DISABLED


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_workflow")
def test_workflow_validation_rejects_capability_mismatch(
    workflow_binding_workspace,
):
    user, tenant, project, client = workflow_binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    bound = bind_capability(installation, "summarize", user)

    definition = _workflow_definition(
        bound.version.id, capability="other-capability"
    )
    workflow = WorkflowDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Mismatch workflow",
        slug="marketplace-mismatch",
        draft_definition=definition,
    )
    response = client.post(
        f"/api/v1/workflows/{workflow.id}/publish/", {}, format="json"
    )
    assert response.status_code == 400
    assert "capability" in response.data["detail"].lower()
    assert WorkflowRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.urls("tests.test_plugin_agent_workflow")
def test_old_pinned_version_fails_on_plugin_version_mismatch(
    workflow_binding_workspace, monkeypatch
):
    user, tenant, project, client = workflow_binding_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    first = bind_capability(installation, "summarize", user)

    installation.plugin_version = "1.1.0"
    installation.save(update_fields=["plugin_version", "updated_at"])
    second = bind_capability(installation, "summarize", user)
    assert second.version.version == 2

    # A workflow still pinned to the first version must fail explicitly
    # instead of silently following the upgraded installation.
    workflow = _publish(
        client,
        tenant,
        project,
        user,
        _workflow_definition(first.version.id),
        "marketplace-pinned-old",
    )
    run_id = _start_run(client, workflow, "wf-marketplace-old-version-001")

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "should not be called"}):
            assert process_once("wf-marketplace-worker") is True

    run = WorkflowRun.objects.select_related("task").get(pk=run_id)
    assert run.task.status == Task.Status.FAILED
    step = WorkflowStepRun.objects.get(workflow_run=run, node_id="summarize")
    assert step.status == WorkflowStepRun.Status.FAILED
    assert step.error["code"] == BINDING_VERSION_MISMATCH

    old_version = AgentVersion.objects.get(pk=first.version.id)
    assert old_version.config_snapshot["plugin_version"] == "1.0.0"
