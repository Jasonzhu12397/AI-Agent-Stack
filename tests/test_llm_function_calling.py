import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.llm_tools import (
    ToolCallValidationError,
    parse_openai_tool_calls,
    to_openai_function_tools,
)
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import ModelConfig, ModelProvider
from django.test import override_settings


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return FakeResponse(self.payload)


def model_config(capabilities=None):
    provider = ModelProvider(
        name="Local model",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="http://ollama:11434/v1",
        credential_ref="",
    )
    return ModelConfig(
        provider=provider,
        model_name="qwen2.5:14b",
        capabilities=capabilities or ["chat", "tools"],
        max_output_tokens=1024,
    )


def test_model_gateway_sends_function_tools_and_returns_tool_calls():
    offered = [
        {
            "type": "function",
            "function": {
                "name": "demo__lookup",
                "description": "Look up a demo value",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "demo__lookup", "arguments": '{"key":"status"}'},
        }
    ]
    opener = FakeOpener(
        {
            "choices": [{"message": {"content": None, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
    )
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        with patch("core.model_gateway.build_opener", return_value=opener):
            response = ModelGateway().complete(
                model_config(),
                [{"role": "user", "content": "check status"}],
                tools=offered,
                tool_choice="auto",
            )

    body = json.loads(opener.request.data.decode("utf-8"))
    assert body["tools"] == offered
    assert body["tool_choice"] == "auto"
    assert response.content == ""
    assert response.tool_calls == tool_calls
    assert response.input_tokens == 10


def test_model_gateway_rejects_tools_without_declared_capability():
    with pytest.raises(ModelGatewayError) as error:
        ModelGateway().complete(
            model_config(["chat"]),
            [{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "demo__lookup"}}],
        )

    assert error.value.code == "MODEL_CAPABILITY_MISSING"


def test_model_gateway_accepts_assistant_tool_and_tool_result_messages():
    opener = FakeOpener(
        {"choices": [{"message": {"content": "done"}}], "usage": {}}
    )
    messages = [
        {"role": "user", "content": "look up status"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "demo__lookup", "arguments": '{"key":"status"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"value":"ready"}'},
    ]
    with override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"]):
        with patch("core.model_gateway.build_opener", return_value=opener):
            response = ModelGateway().complete(model_config(), messages)

    assert response.content == "done"


def test_tool_schema_conversion_and_tool_call_validation():
    plugin = SimpleNamespace(plugin_key="demo", name="Demo")
    tool = SimpleNamespace(
        enabled=True,
        plugin=plugin,
        name="lookup",
        description="Look up a value",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    )
    offered = to_openai_function_tools([tool])
    calls = parse_openai_tool_calls(
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "demo__lookup",
                        "arguments": '{"key":"status"}',
                    },
                }
            ]
        },
        offered,
    )

    assert offered[0]["function"]["name"] == "demo__lookup"
    assert calls == [{"id": "call-1", "name": "demo__lookup", "arguments": {"key": "status"}}]


def test_tool_call_validation_rejects_unoffered_function_and_non_object_arguments():
    offered = [
        {
            "type": "function",
            "function": {"name": "demo__lookup"},
        }
    ]
    with pytest.raises(ToolCallValidationError):
        parse_openai_tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "demo__delete",
                            "arguments": "{}",
                        },
                    }
                ]
            },
            offered,
        )
    with pytest.raises(ToolCallValidationError):
        parse_openai_tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call-2",
                        "function": {
                            "name": "demo__lookup",
                            "arguments": "[]",
                        },
                    }
                ]
            },
            offered,
        )
