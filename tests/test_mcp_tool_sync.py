import uuid

import pytest
from core.mcp_connector import ToolExecutionError
from core.mcp_tool_sync import sync_mcp_tools
from core.models import PluginDefinition, PluginTool, Tenant
from django.contrib.auth import get_user_model
from django.urls import resolve


def _plugin(connector_type=PluginDefinition.ConnectorType.MCP, version="1.0.0"):
    user = get_user_model().objects.create_user(
        username=f"mcp-sync-{uuid.uuid4().hex[:8]}",
        password="test-password",
    )
    tenant = Tenant.objects.create(
        name="MCP sync tenant",
        slug=f"mcp-sync-{uuid.uuid4().hex[:8]}",
    )
    return PluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=f"fake-mcp-{uuid.uuid4().hex[:6]}",
        name="Fake MCP",
        version=version,
        connector_type=connector_type,
        manifest={"mcp": {"endpoint_url": "https://mcp.example.com/rpc"}},
    )


@pytest.mark.django_db
def test_sync_imports_new_tools_as_disabled(monkeypatch):
    plugin = _plugin()
    monkeypatch.setattr(
        "core.mcp_tool_sync.discover_mcp_tools",
        lambda context: [
            {
                "name": "remote_search",
                "description": "Search hosts",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
            {"name": "remote_cleanup", "description": "", "input_schema": {}},
        ],
    )

    summary = sync_mcp_tools(plugin, object())

    assert summary == {
        "created": ["remote_search", "remote_cleanup"],
        "updated": [],
        "unchanged": [],
    }
    tool = PluginTool.objects.get(plugin=plugin, name="remote_search")
    assert tool.enabled is False
    assert tool.input_schema["properties"] == {"query": {"type": "string"}}


@pytest.mark.django_db
def test_sync_is_idempotent_and_refreshes_changed_schemas(monkeypatch):
    plugin = _plugin()
    descriptors = [
        {"name": "remote_search", "description": "Search hosts", "input_schema": {"type": "object"}}
    ]
    monkeypatch.setattr("core.mcp_tool_sync.discover_mcp_tools", lambda context: descriptors)
    sync_mcp_tools(plugin, object())

    summary = sync_mcp_tools(plugin, object())
    assert summary == {"created": [], "updated": [], "unchanged": ["remote_search"]}

    descriptors[0]["input_schema"] = {"type": "object", "required": ["query"]}
    descriptors[0]["description"] = "Search hosts by name"
    summary = sync_mcp_tools(plugin, object())
    assert summary == {"created": [], "updated": ["remote_search"], "unchanged": []}
    tool = PluginTool.objects.get(plugin=plugin, name="remote_search")
    assert tool.input_schema["required"] == ["query"]
    assert tool.description == "Search hosts by name"
    assert PluginTool.objects.filter(plugin=plugin).count() == 1


@pytest.mark.django_db
def test_sync_requires_an_mcp_connector(monkeypatch):
    plugin = _plugin(connector_type=PluginDefinition.ConnectorType.HTTP)
    monkeypatch.setattr(
        "core.mcp_tool_sync.discover_mcp_tools",
        lambda context: [{"name": "remote_search", "input_schema": {}}],
    )
    with pytest.raises(ToolExecutionError, match="MCP connector"):
        sync_mcp_tools(plugin, object())
    assert PluginTool.objects.filter(plugin=plugin).count() == 0


@pytest.mark.django_db
def test_sync_rejects_unusable_tool_descriptors(monkeypatch):
    plugin = _plugin()
    for descriptor in (
        {"name": "Remote/Search", "input_schema": {}},
        {"name": "", "input_schema": {}},
        {"name": "remote_search", "input_schema": []},
    ):
        monkeypatch.setattr(
            "core.mcp_tool_sync.discover_mcp_tools",
            lambda context, payload=descriptor: [payload],
        )
        with pytest.raises(ToolExecutionError):
            sync_mcp_tools(plugin, object())
    assert PluginTool.objects.filter(plugin=plugin).count() == 0


@pytest.mark.django_db
def test_sync_rejects_oversized_tool_sets(monkeypatch):
    plugin = _plugin()
    monkeypatch.setattr(
        "core.mcp_tool_sync.discover_mcp_tools",
        lambda context: [{"name": f"tool_{index}", "input_schema": {}} for index in range(257)],
    )
    with pytest.raises(ToolExecutionError, match="too many tools"):
        sync_mcp_tools(plugin, object())


def test_tool_sync_endpoint_is_routed():
    match = resolve(f"/api/v1/plugin-connections/{uuid.uuid4()}/sync-tools/")
    assert match.url_name == "plugin-connection-sync-tools"
