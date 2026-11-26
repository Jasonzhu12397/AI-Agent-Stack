import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from core import mcp_connector
from core.tool_router import FAULT_TIMEOUT, FAULT_UNREACHABLE, ToolExecutionError
from django.test import override_settings

STUB = str(Path(__file__).with_name("mcp_stdio_stub.py"))


def _manifest(**mcp):
    return {
        "mcp": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [STUB],
            **mcp,
        },
        "timeout_seconds": 10,
    }


def _context(manifest):
    return SimpleNamespace(plugin_manifest=manifest)


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[sys.executable])
def test_stdio_discovery_and_call_round_trip():
    context = _context(_manifest())
    tools = mcp_connector.discover_mcp_tools(context)
    assert [tool["name"] for tool in tools] == ["echo"]
    assert tools[0]["description"] == "Echo the provided text."

    tool = SimpleNamespace(name="echo")
    result = mcp_connector.execute_mcp_operation({"text": "hello stdio"}, context, tool)
    assert result["content"] == [{"type": "text", "text": "hello stdio"}]
    assert result["isError"] is False


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[sys.executable])
def test_stdio_tool_map_remaps_remote_name():
    context = _context(_manifest(tool_map={"local_echo": "echo"}))
    tool = SimpleNamespace(name="local_echo")
    result = mcp_connector.execute_mcp_operation({"text": "mapped"}, context, tool)
    assert result["content"][0]["text"] == "mapped"


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[])
def test_stdio_is_disabled_without_allowlisted_commands():
    context = _context(_manifest())
    with pytest.raises(ToolExecutionError, match="MCP stdio is disabled"):
        mcp_connector.discover_mcp_tools(context)


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=["not-the-python-binary"])
def test_stdio_rejects_a_command_outside_the_allowlist():
    context = _context(_manifest())
    with pytest.raises(ToolExecutionError, match="not in MCP_STDIO_ALLOWED_COMMANDS"):
        mcp_connector.discover_mcp_tools(context)


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[sys.executable])
def test_stdio_env_values_must_be_env_references():
    context = _context(_manifest(env={"EXAMPLE_TOKEN": "literal-secret"}))
    with pytest.raises(ToolExecutionError, match="env: references"):
        mcp_connector.discover_mcp_tools(context)


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[sys.executable])
def test_stdio_caller_timeout_is_enforced():
    context = _context(_manifest())
    tool = SimpleNamespace(name="echo", timeout_seconds=1)
    with pytest.raises(ToolExecutionError) as excinfo:
        mcp_connector.execute_mcp_operation({"text": "slow", "sleep": 5}, context, tool)
    assert excinfo.value.code == FAULT_TIMEOUT


@override_settings(MCP_STDIO_ALLOWED_COMMANDS=[sys.executable, "/bin/false"])
def test_stdio_non_zero_exit_is_unreachable():
    context = _context(_manifest(command="/bin/false", args=[]))
    with pytest.raises(ToolExecutionError) as excinfo:
        mcp_connector.discover_mcp_tools(context)
    assert excinfo.value.code == FAULT_UNREACHABLE
