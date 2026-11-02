from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=500)


class FakeModel:
    """Deterministic model fixture; its output is still untrusted input."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def choose_tool(self) -> ToolIntent:
        return ToolIntent.model_validate(self._response)


class FakeTool:
    """A side-effect-free tool fixture for local tests."""

    name = "echo"

    def invoke(self, arguments: dict[str, Any]) -> dict[str, str]:
        parsed = EchoInput.model_validate(arguments)
        return {"value": parsed.value}


class FakeMCPServer:
    """In-process MCP-shaped fixture, not an implementation of the MCP protocol."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register_tool(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        if not name or name in self._tools:
            raise ValueError("tool name must be non-empty and unique")
        self._tools[name] = handler

    def list_tools(self) -> list[str]:
        return sorted(self._tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._tools.get(name)
        if handler is None:
            raise KeyError(f"unknown fake MCP tool: {name}")
        result = handler(arguments)
        if not isinstance(result, dict):
            raise TypeError("fake MCP tool output must be an object")
        return result
