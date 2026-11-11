import uuid
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
    ToolApprovalRequest,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from worker_app import tasks as worker_tasks


@pytest.mark.django_db
@pytest.mark.parametrize(
    "risk_level,expire_after_tool",
    [("low", False), ("low", True), ("high", False)],
)
def test_agent_worker_executes_tool_then_returns_final_model_answer(
    risk_level,
    expire_after_tool,
):
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
        risk_level=risk_level,
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
    version = AgentVersion.objects.create(
        agent=agent,
        version=1,
        config_snapshot={
            "model_config_id": str(model.id),
            "system_prompt": "Use tools when required.",
            "tool_allowlist": [str(tool.id)],
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
        status=Task.Status.RUNNING,
        lease_owner="worker-test",
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() + timezone.timedelta(minutes=5),
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

    def lookup_handler(arguments, context):
        if expire_after_tool:
            Task.objects.filter(pk=task.pk).update(
                lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
            )
        return {"value": f"{arguments['key']}-ready"}

    worker_tasks.TOOL_ROUTER._handlers[("demo", "lookup")] = lookup_handler
    try:
        with patch.object(worker_tasks, "ModelGateway", return_value=fake_gateway):
            if risk_level == "high":
                with pytest.raises(
                    worker_tasks.TaskSuspendedForApproval,
                    match="waiting for explicit connector tool approval",
                ):
                    worker_tasks.execute_agent_run(task)
            elif expire_after_tool:
                with pytest.raises(
                    worker_tasks.PermanentTaskError,
                    match="lease was lost or cancellation was requested",
                ):
                    worker_tasks.execute_agent_run(task)
            else:
                result = worker_tasks.execute_agent_run(task)
    finally:
        worker_tasks.TOOL_ROUTER._handlers = handlers

    assert fake_gateway.calls[0]["kwargs"]["tools"][0]["function"]["name"] == "demo__lookup"
    if expire_after_tool:
        task.refresh_from_db()
        assert task.status == Task.Status.RUNNING
        assert task.lease_expires_at < timezone.now()
        assert len(fake_gateway.calls) == 1
        return

    if risk_level == "high":
        task.refresh_from_db()
        approval = ToolApprovalRequest.objects.get(task=task, call_id="call-1")
        assert task.status == Task.Status.WAITING_APPROVAL
        assert task.lease_token is None
        assert approval.status == ToolApprovalRequest.Status.PENDING
        assert approval.tool_id == tool.id
        assert approval.installation_id == installation.id
        assert approval.arguments == {"key": "service-status"}
        assert approval.execution_checkpoint["pending_calls"][0]["id"] == "call-1"
        assert approval.execution_checkpoint["messages"][-1]["role"] == "assistant"
        assert len(fake_gateway.calls) == 1
        assert fake_gateway.calls[0]["messages"][-1]["role"] == "user"

        approval.status = ToolApprovalRequest.Status.APPROVED
        approval.approved_by = user
        approval.approved_at = timezone.now()
        approval.save(update_fields=["status", "approved_by", "approved_at"])
        task.status = Task.Status.RUNNING
        task.lease_owner = "worker-resume"
        task.lease_token = uuid.uuid4()
        task.lease_expires_at = timezone.now() + timezone.timedelta(minutes=5)
        task.save(
            update_fields=[
                "status",
                "lease_owner",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        worker_tasks.TOOL_ROUTER._handlers[("demo", "lookup")] = lookup_handler
        try:
            with patch.object(worker_tasks, "ModelGateway", return_value=fake_gateway):
                resumed_result = worker_tasks.execute_agent_run(task)
        finally:
            worker_tasks.TOOL_ROUTER._handlers = handlers

        approval.refresh_from_db()
        assert approval.status == ToolApprovalRequest.Status.CONSUMED
        assert approval.consumed_at is not None
        assert resumed_result["content"] == "The service is ready."
        assert resumed_result["tool_calls"] == 1
        assert resumed_result["input_tokens"] == 27
        assert len(fake_gateway.calls) == 2
        resumed_messages = fake_gateway.calls[1]["messages"]
        assert resumed_messages[-1]["role"] == "tool"
        assert "service-status-ready" in resumed_messages[-1]["content"]
        assert not worker_tasks.consume_approved_tool_call(
            task=task,
            tool=tool,
            installation=installation,
            connection=None,
            call_id="call-1",
            arguments={"key": "service-status"},
        )
        return

    assert result["content"] == "The service is ready."
    assert result["tool_calls"] == 1
    assert result["input_tokens"] == 27
    assert len(fake_gateway.calls) == 2
    second_turn_messages = fake_gateway.calls[1]["messages"]
    assert second_turn_messages[-1]["role"] == "tool"
    assert "service-status-ready" in second_turn_messages[-1]["content"]
