import json

from worker_app.tasks import (
    MAX_AGENT_TOOL_RESULT_BYTES,
    _serialize_agent_tool_result,
)


def test_agent_tool_result_serializes_json_values():
    serialized = _serialize_agent_tool_result({"ok": True, "message": "完成"})

    assert json.loads(serialized) == {"ok": True, "message": "完成"}


def test_agent_tool_result_rejects_non_finite_numbers():
    serialized = _serialize_agent_tool_result({"value": float("nan")})

    assert json.loads(serialized) == {
        "ok": False,
        "error": "Tool returned a result that could not be serialized.",
    }


def test_agent_tool_result_omits_payload_over_context_limit():
    serialized = _serialize_agent_tool_result({"payload": "x" * MAX_AGENT_TOOL_RESULT_BYTES})

    result = json.loads(serialized)
    assert result["ok"] is False
    assert "32 KiB" in result["error"]


def test_agent_tool_result_handles_circular_payloads():
    payload = {}
    payload["self"] = payload

    serialized = _serialize_agent_tool_result(payload)

    assert json.loads(serialized)["ok"] is False
