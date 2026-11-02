import pytest
from app.fakes import EchoInput, FakeMCPServer, FakeModel, FakeTool, ToolIntent


def test_fake_model_returns_validated_structured_intent() -> None:
    model = FakeModel({"tool_name": "echo", "arguments": {"value": "hello"}})

    intent = model.choose_tool()

    assert intent == ToolIntent(tool_name="echo", arguments={"value": "hello"})


def test_fake_model_rejects_unexpected_fields() -> None:
    model = FakeModel(
        {"tool_name": "echo", "arguments": {"value": "hello"}, "authorized": True}
    )

    with pytest.raises(ValueError):
        model.choose_tool()


def test_fake_tool_validates_input_and_has_no_external_side_effect() -> None:
    tool = FakeTool()

    assert tool.invoke({"value": "local-only"}) == {"value": "local-only"}

    with pytest.raises(ValueError):
        tool.invoke({"value": ""})
    with pytest.raises(ValidationError):
        EchoInput.model_validate({"value": "ok", "tenant_id": "spoofed"})


def test_fake_mcp_server_registers_lists_and_calls_tools() -> None:
    server = FakeMCPServer()
    server.register_tool("echo", FakeTool().invoke)

    assert server.list_tools() == ["echo"]
    assert server.call_tool("echo", {"value": "fixture"}) == {"value": "fixture"}


def test_fake_mcp_server_rejects_unknown_or_duplicate_tools() -> None:
    server = FakeMCPServer()
    server.register_tool("echo", FakeTool().invoke)

    with pytest.raises(KeyError):
        server.call_tool("admin", {})
    with pytest.raises(ValueError):
        server.register_tool("echo", FakeTool().invoke)
