import pytest
from worker_app.tasks import (
    MAX_AGENT_RUN_OUTPUT_TOKENS,
    MAX_AGENT_RUN_TOTAL_TOKENS,
    MAX_AGENT_TOOL_CALLS,
    MAX_AGENT_TOOL_RESULT_BYTES,
    PermanentTaskError,
    _allowed_agent_output_tokens,
    _compact_agent_messages,
    _estimate_message_tokens,
)


def test_agent_output_budget_is_clamped_by_run_and_model_limits():
    allowed = _allowed_agent_output_tokens(
        configured_max_tokens=8192,
        model_max_tokens=4096,
        input_tokens=2000,
        output_tokens=100,
        estimated_input_tokens=3000,
    )

    assert allowed == min(
        4096,
        MAX_AGENT_RUN_OUTPUT_TOKENS - 100,
        MAX_AGENT_RUN_TOTAL_TOKENS - 2000 - 100 - 3000,
    )


def test_agent_output_budget_stops_before_call_when_run_budget_is_exhausted():
    allowed = _allowed_agent_output_tokens(
        configured_max_tokens=1024,
        model_max_tokens=1024,
        input_tokens=15000,
        output_tokens=900,
        estimated_input_tokens=200,
    )

    assert allowed < 1


def test_agent_prompt_estimator_rejects_oversized_context():
    messages = [{"role": "user", "content": "x" * 25000}]

    with pytest.raises(PermanentTaskError, match="24 KiB input budget"):
        _estimate_message_tokens(messages)


def test_agent_prompt_budget_includes_tool_schemas():
    messages = [{"role": "user", "content": "short request"}]
    tools = [{"type": "function", "function": {"name": "large_schema", "parameters": {
        "description": "x" * 25000
    }}}]

    with pytest.raises(PermanentTaskError, match="prompt and tool schemas"):
        _estimate_message_tokens(messages, tools)


def test_agent_prompt_estimator_counts_tool_schema_tokens():
    messages = [{"role": "user", "content": "short request"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "x" * 500}},
    }}}]

    assert _estimate_message_tokens(messages, tools) > _estimate_message_tokens(messages)


def test_agent_message_compaction_keeps_system_prompt_and_original_request():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original request"},
        {"role": "assistant", "content": "old response"},
        {"role": "tool", "tool_call_id": "1", "content": "old tool result"},
        {"role": "assistant", "content": "recent response"},
        {"role": "tool", "tool_call_id": "2", "content": "recent tool result"},
        {"role": "assistant", "content": "latest response"},
    ]

    compacted = _compact_agent_messages(messages)

    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert messages[2] not in compacted
    assert messages[-1] in compacted


def test_agent_tool_and_token_budgets_are_hard_bounded():
    assert MAX_AGENT_TOOL_CALLS == 12
    assert MAX_AGENT_TOOL_RESULT_BYTES == 8192
    assert MAX_AGENT_RUN_OUTPUT_TOKENS == 2048
    assert MAX_AGENT_RUN_TOTAL_TOKENS == 16000
