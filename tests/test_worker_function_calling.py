from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    ModelConfig,
    ModelProvider,
    PluginDefinition,
    PluginInstallation,
    PluginTool,
    Project,
    Task,
    Tenant,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from worker_app import tasks as worker_tasks


@pytest.mark.django_db
def test_agent_worker_executes_tool_then_returns_final_model_answer():
    user = get_user_model().objects.create_user(username="tool-agent-user")
    tenant = Tenant.objects.create(name="Tool tenant", slug="tool-tenant")
    project = Project.objects.create(tenant=tenant, name="Tool project", slug="tool-project")
    agent = AgentDefinition.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="Tool agent",
        slug="tool-agent",
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Tool model",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="test-tool-model",
        display_name="Test tool model",
        capabilities=["chat", "tools"],
        max_output_tokens=512,
    )
    plugin = PluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="demo",
        name="Demo connector",
        version="1.0.0",
        connector_type=PluginDefinition.ConnectorType.NATIVE,
        review_status=PluginDefinition.ReviewStatus.APPROVED,
        enabled=True,
    )
    tool = PluginTool.objects.create(
        plugin=plugin,
        name="lookup",
        description="Look up a value",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        enabled=True,
    )
    PluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=PluginInstallation.Status.INSTALLED,
        granted_permissions=[],
        installed_by=user,
    )
    version = AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot={
            "model_config_id": str(model.id),
            "system_prompt": "Use tools when required.",
            "tool_allowlist": [str(tool.id)],
            "max_iterations": 3,
            "timeout_seconds": 30,
            "max_tokens": 256,
        },
        created_by=user,
    )
    task = Task.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        task_type="agent.run",
        payload={},
        idempotency_key="tool-agent-run",
        available_at=timezone.now(),
    )
    AgentRun.objects.create(
        task=task,
        agent_version=version,
        input_text="Look up the service status.",
    )

    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "demo__lookup",
            "arguments": '{"key":"service-status"}',
        },
    }

    class FakeGateway:
        def __init__(self):
            self.calls = []

        def complete(self, model_config, messages, **kwargs):
            self.calls.append({"messages": list(messages), "kwargs": kwargs})
            if len(self.calls) == 1:
                return SimpleNamespace(
                    content="",
                    tool_calls=[tool_call],
                    input_tokens=12,
                    output_tokens=5,
                    latency_ms=8,
                    provider="fake",
                    model="test-tool-model",
                )
            return SimpleNamespace(
                content="The service is ready.",
                tool_calls=[],
                input_tokens=15,
                output_tokens=7,
                latency_ms=9,
                provider="fake",
                model="test-tool-model",
            )

    fake_gateway = FakeGateway()
    handlers = worker_tasks.TOOL_ROUTER._handlers.copy()
    worker_tasks.TOOL_ROUTER._handlers[("demo", "lookup")] = (
        lambda arguments, context: {"value": f"{arguments['key']}-ready"}
    )
    try:
        with patch.object(worker_tasks, "ModelGateway", return_value=fake_gateway):
            result = worker_tasks.execute_agent_run(task)
    finally:
        worker_tasks.TOOL_ROUTER._handlers = handlers

    assert result["content"] == "The service is ready."
    assert result["tool_calls"] == 1
    assert result["input_tokens"] == 27
    assert len(fake_gateway.calls) == 2
    second_turn_messages = fake_gateway.calls[1]["messages"]
    assert second_turn_messages[-1]["role"] == "tool"
    assert "service-status-ready" in second_turn_messages[-1]["content"]
    assert fake_gateway.calls[0]["kwargs"]["tools"][0]["function"]["name"] == "demo__lookup"
