# ruff: noqa: I001
from types import SimpleNamespace

import pytest

from core.tool_approvals import (
    approval_matches_call,
    canonical_tool_arguments,
    tool_arguments_hash,
    validate_execution_checkpoint,
)


def test_canonical_arguments_are_order_independent():
    first = {"outer": {"b": 2, "a": 1}, "enabled": True}
    second = {"enabled": True, "outer": {"a": 1, "b": 2}}

    assert canonical_tool_arguments(first) == canonical_tool_arguments(second)
    assert tool_arguments_hash(first) == tool_arguments_hash(second)


def test_argument_hash_preserves_json_scalar_types():
    assert tool_arguments_hash({"value": True}) != tool_arguments_hash({"value": 1})
    assert tool_arguments_hash({"value": 1}) != tool_arguments_hash({"value": 1.0})


@pytest.mark.parametrize("arguments", [[], "text", None, {"value": float("nan")}])
def test_canonical_arguments_reject_non_object_or_non_finite_values(arguments):
    with pytest.raises(ValueError):
        canonical_tool_arguments(arguments)


def test_approval_matches_exact_task_tool_installation_connection_and_arguments():
    tenant_id = "tenant-1"
    project_id = "project-1"
    task = SimpleNamespace(id="task-1", tenant_id=tenant_id, project_id=project_id)
    plugin = SimpleNamespace(version="2.1.0")
    tool = SimpleNamespace(id="tool-1", plugin=plugin)
    installation = SimpleNamespace(id="installation-1")
    connection = SimpleNamespace(id="connection-1")
    arguments = {"resource": "cluster-a", "force": False}
    approval = SimpleNamespace(
        task_id=task.id,
        tenant_id=tenant_id,
        project_id=project_id,
        tool_id=tool.id,
        installation_id=installation.id,
        connection_id=connection.id,
        plugin_version=plugin.version,
        arguments_hash=tool_arguments_hash(arguments),
    )

    assert approval_matches_call(
        approval,
        task=task,
        tool=tool,
        installation=installation,
        connection=connection,
        arguments=arguments,
    )
    assert not approval_matches_call(
        approval,
        task=task,
        tool=tool,
        installation=installation,
        connection=connection,
        arguments={"resource": "cluster-b", "force": False},
    )
    assert not approval_matches_call(
        approval,
        task=task,
        tool=tool,
        installation=installation,
        connection=None,
        arguments=arguments,
    )



def test_execution_checkpoint_accepts_bounded_json_object():
    checkpoint = {
        "messages": [
            {"role": "assistant", "content": None},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "connector__update",
                            "arguments": '{"enabled":true}',
                        },
                    }
                ],
            },
        ],
        "input_tokens": 128,
        "output_tokens": 32,
    }

    assert validate_execution_checkpoint(checkpoint) is checkpoint


@pytest.mark.parametrize(
    "checkpoint",
    [
        [],
        "not-an-object",
        {"value": float("inf")},
    ],
)
def test_execution_checkpoint_rejects_invalid_json(checkpoint):
    with pytest.raises(ValueError):
        validate_execution_checkpoint(checkpoint)


def test_execution_checkpoint_rejects_payload_over_64_kib():
    with pytest.raises(ValueError, match="64 KiB"):
        validate_execution_checkpoint({"content": "x" * 65536})
