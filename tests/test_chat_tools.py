"""The console turn executes tools instead of describing them."""


import pytest
from core import chat_runtime, chat_tools
from core.chat_models import AgentChatThread
from core.models import (
    AgentCommand,
    AgentNode,
    AgentPluginDefinition,
    AgentPluginInstallation,
    ClusterDeployment,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone


@pytest.fixture
def scope(db):
    user = get_user_model().objects.create_user(username="tool-owner")
    tenant = Tenant.objects.create(name="Tool tenant", slug="tool-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Tool project", slug="tool-project")
    thread = AgentChatThread.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        model_config=ModelConfig.objects.create(
            provider=ModelProvider.objects.create(
                tenant=tenant,
                created_by=user,
                name="Tool provider",
                provider_type=ModelProvider.ProviderType.FAKE,
                config={"response": "ok"},
            ),
            model_name="tool-fake",
            display_name="Tool Fake",
            capabilities=["chat", "tools"],
            max_output_tokens=1024,
        ),
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="eph-1",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash="x" * 64,
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    return {"user": user, "tenant": tenant, "project": project, "thread": thread, "node": node}


def _plugin(scope, plugin_key, capabilities, auth_required=True, credential_ref="vault://x"):
    plugin = AgentPluginDefinition.objects.create(
        tenant=scope["tenant"],
        created_by=scope["user"],
        plugin_key=plugin_key,
        name=plugin_key.title(),
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.HTTP_JSON,
        endpoint_url="http://127.0.0.1:9/unreachable",
        auth_required=auth_required,
        capabilities=capabilities,
    )
    installation = AgentPluginInstallation.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        installed_by=scope["user"],
        plugin=plugin,
        plugin_version=plugin.version,
        status=AgentPluginInstallation.Status.INSTALLED,
        credential_ref=credential_ref if auth_required else "",
        granted_capabilities=[item["name"] for item in capabilities],
    )
    return plugin, installation


def test_platform_tools_are_always_offered(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    names = {tool.name for tool in tools}
    assert {
        "stack_list_nodes",
        "stack_list_deployments",
        "stack_list_commands",
        "stack_list_registry_images",
        "stack_list_scheduled_jobs",
        "stack_create_command",
    } <= names
    offered = chat_tools.to_openai_tools(
        [tool for tool in tools if tool.name == "stack_list_nodes"]
    )
    assert offered[0]["function"]["parameters"]["type"] == "object"


def test_skill_selection_offers_only_that_capability(scope):
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    _plugin(
        scope,
        "github-agent",
        [
            {"name": "issue.read", "description": "Read an issue", "input_schema": schema},
            {"name": "pr.open", "description": "Open a PR", "input_schema": schema},
        ],
    )
    by_skill = chat_tools.build_tools(scope["thread"], {"skills": ["issue.read"]})
    plugin_tools = [tool for tool in by_skill if tool.kind == "plugin"]
    assert [tool.identifier for tool in plugin_tools] == ["issue.read"]
    assert plugin_tools[0].name == "github-agent__issue_read"

    installation_id = str(
        AgentPluginInstallation.objects.get(plugin__plugin_key="github-agent").id
    )
    by_plugin = chat_tools.build_tools(scope["thread"], {"plugins": [installation_id]})
    plugin_tools = [tool for tool in by_plugin if tool.kind == "plugin"]
    assert {tool.identifier for tool in plugin_tools} == {"issue.read", "pr.open"}


def test_list_nodes_reads_the_control_plane(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "stack_list_nodes")
    payload = chat_tools.run_tool(tool, {})
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["nodes"][0]["name"] == "eph-1"
    assert payload["nodes"][0]["status"] == "online"


def test_create_command_files_a_real_command_and_gates_cluster_changes(scope):
    tool = next(
        item
        for item in chat_tools.build_tools(scope["thread"], {})
        if item.name == "stack_create_command"
    )
    payload = chat_tools.run_tool(
        tool,
        {
            "node_id": str(scope["node"].id),
            "command_type": "cluster.kubernetes.version.upgrade",
            "payload": {"version": "v1.31.4"},
        },
    )
    assert payload["ok"] is True
    assert payload["command"]["requires_approval"] is True
    assert payload["command"]["status"] == AgentCommand.Status.WAITING_APPROVAL
    command = AgentCommand.objects.get(pk=payload["command"]["id"])
    assert command.project_id == scope["project"].id
    assert command.requested_by_id == scope["user"].id

    # The same request is idempotent: a retried turn must not queue twice.
    again = chat_tools.run_tool(
        tool,
        {
            "node_id": str(scope["node"].id),
            "command_type": "cluster.kubernetes.version.upgrade",
            "payload": {"version": "v1.31.4"},
        },
    )
    assert again["created"] is False
    assert AgentCommand.objects.count() == 1


def test_create_command_rejects_an_unknown_node(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "stack_create_command")
    payload = chat_tools.run_tool(
        tool,
        {"node_id": "00000000-0000-0000-0000-000000000000", "command_type": "probe.run"},
    )
    assert payload == {
        "ok": False,
        "code": "NODE_NOT_FOUND",
        "error": "No enrolled node with that id in this project.",
    }


def test_plugin_capability_tool_surfaces_the_adapter_error(scope):
    _plugin(
        scope,
        "vault-agent",
        [
            {
                "name": "secret.read",
                "description": "Read one secret",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "risk_level": "low",
            }
        ],
    )
    tool = next(
        item
        for item in chat_tools.build_tools(scope["thread"], {"skills": ["secret.read"]})
        if item.kind == "plugin"
    )
    payload = chat_tools.run_tool(tool, {"text": "db/password"})
    # The endpoint is deliberately unreachable: the call really happened and the
    # adapter reported the fault instead of the console pretending.
    assert payload["ok"] is False
    assert payload["code"].startswith("CAPABILITY_")


class _FakeResult:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self._usage = {"input_tokens": 3, "output_tokens": 5, "latency_ms": 11}

    def as_dict(self):
        return {"usage": self._usage}


def test_run_turn_executes_tool_calls_and_feeds_results_back(scope, monkeypatch):
    calls = []

    def fake_complete(_tenant, _model, messages, **kwargs):
        calls.append({"messages": list(messages), "tools": kwargs.get("tools")})
        if len(calls) == 1:
            return _FakeResult(
                "I will check.",
                [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "stack_list_nodes", "arguments": "{}"},
                    }
                ],
            )
        return _FakeResult("One node is online: eph-1.")

    monkeypatch.setattr(chat_runtime, "_complete", fake_complete)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "list nodes"}]
    content, usage, tool_log = chat_runtime.run_turn(
        scope["thread"],
        scope["thread"].model_config,
        user=scope["user"],
        messages=messages,
        context={},
        idempotency_key="turn-1",
    )
    assert content == "One node is online: eph-1."
    assert usage["tool_calls"] == 1
    assert tool_log[0]["name"] == "stack_list_nodes"
    assert tool_log[0]["ok"] is True
    assert any(message["role"] == "tool" for message in messages)
    assert calls[0]["tools"][0]["type"] == "function"


def test_run_turn_without_tool_support_does_not_offer_functions(scope, monkeypatch):
    model = scope["thread"].model_config
    model.capabilities = ["chat"]
    model.save(update_fields=["capabilities"])
    seen = {}

    def fake_complete(_tenant, _model, messages, **kwargs):
        seen["tools"] = kwargs.get("tools")
        return _FakeResult("No tools here.")

    monkeypatch.setattr(chat_runtime, "_complete", fake_complete)
    content, usage, tool_log = chat_runtime.run_turn(
        scope["thread"],
        model,
        user=scope["user"],
        messages=[{"role": "user", "content": "hi"}],
        context={},
        idempotency_key="turn-2",
    )
    assert content == "No tools here."
    assert seen["tools"] is None
    assert usage["tools_offered"] == 0
    assert usage["tools_note"] == "model does not support tool calls"
    assert tool_log == []


def test_turn_budget_is_bounded(scope, monkeypatch):
    state = {"n": 0}

    def always_calls(_tenant, _model, messages, **kwargs):
        state["n"] += 1
        return _FakeResult(
            "again",
            [
                {
                    "id": f"call-{state['n']}",
                    "type": "function",
                    "function": {"name": "stack_list_nodes", "arguments": "{}"},
                }
            ],
        )

    monkeypatch.setattr(chat_runtime, "_complete", always_calls)
    content, usage, tool_log = chat_runtime.run_turn(
        scope["thread"],
        scope["thread"].model_config,
        user=scope["user"],
        messages=[{"role": "user", "content": "loop"}],
        context={},
        idempotency_key="turn-3",
    )
    assert usage["truncated"] is True
    assert len(tool_log) == chat_tools.MAX_TOOL_STEPS


def test_plan_deployment_rejects_an_unknown_eph_node(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "stack_plan_deployment")
    payload = chat_tools.run_tool(
        tool,
        {
            "eph_node_id": "00000000-0000-0000-0000-000000000000",
            "cluster_spec": {"cluster_name": "sample"},
        },
    )
    assert payload["ok"] is False
    assert payload["code"] == "NODE_NOT_FOUND"


def test_plan_deployment_reports_the_spec_error(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "stack_plan_deployment")
    payload = chat_tools.run_tool(
        tool,
        {"eph_node_id": str(scope["node"].id), "cluster_spec": {"cluster_name": "Sample_Cluster"}},
    )
    assert payload["ok"] is False
    assert payload["code"] == "DEPLOYMENT_SPEC_INVALID"
    assert "cluster_name" in payload["error"] or "cluster_spec" in payload["error"]


def test_plan_deployment_files_the_preflight(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "stack_plan_deployment")
    payload = chat_tools.run_tool(
        tool,
        {
            "eph_node_id": str(scope["node"].id),
            "cluster_spec": {
                "cluster_name": "chat-demo",
                "kubernetes_version": "v1.30.2",
                "control_plane_replicas": 1,
                "worker_replicas": 1,
            },
        },
    )
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["deployment"]["status"] == "preflight_queued"
    deployment = ClusterDeployment.objects.get(id=payload["deployment"]["id"])
    assert deployment.preflight_result["ironic_workload"]["warnings"] == [
        "IRONIC_WORKLOAD_CR_REQUIRED"
    ]
