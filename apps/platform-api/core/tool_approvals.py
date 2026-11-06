import hashlib
import json


def canonical_tool_arguments(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Tool arguments must contain finite JSON values") from exc
    return encoded


def tool_arguments_hash(arguments):
    canonical = canonical_tool_arguments(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_matches_call(approval, *, task, tool, installation, connection, arguments):
    if approval.task_id != task.id:
        return False
    if approval.tenant_id != task.tenant_id or approval.project_id != task.project_id:
        return False
    if approval.tool_id != tool.id or approval.installation_id != installation.id:
        return False
    if approval.connection_id != getattr(connection, "id", None):
        return False
    if approval.plugin_version != tool.plugin.version:
        return False
    try:
        return approval.arguments_hash == tool_arguments_hash(arguments)
    except ValueError:
        return False
