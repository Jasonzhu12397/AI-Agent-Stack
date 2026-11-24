"""Tool catalog for the Control Agent console.

A chat turn is not only a completion: the composer lets the member scope the
conversation to specific capabilities and installed plugins, and the answer has
to be able to *use* them. This module builds the OpenAI-compatible function
catalog for one thread and executes the calls the model selects.

Two families exist:

* ``platform`` actions read the tenant's own control plane (nodes, deployments,
  commands, registry images, schedules) and can file a real ``AgentCommand``,
  which lands in ``waiting_approval`` for the commands that require a human.
* ``plugin`` capabilities are the granted capabilities of the installed agent
  plugins, so a skill chosen in the composer becomes a callable function rather
  than a name in a prompt.

Every handler returns a bounded JSON-serialisable payload; failures are returned
as ``{"ok": false, "code": ...}`` so the model can correct itself instead of the
turn collapsing.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from django.utils import timezone

from core.agent_capability_router import CapabilityResolutionError, invoke_capability
from core.chat_models import AgentChatThread
from core.models import (
    AgentCommand,
    AgentNode,
    AgentPluginDefinition,
    AgentPluginInstallation,
    ClusterDeployment,
)
from core.registry_models import RegistryImage
from core.scheduled_models import ScheduledChatJob

#: A provider rejects function names outside this alphabet, and a chat answer is
#: not an agent run: four steps is enough to read, decide and answer.
MAX_TOOL_STEPS = 4
MAX_TOOL_CALLS = 16
MAX_TOOL_RESULT_CHARS = 8000

#: Commands the control plane refuses to queue without a human approval. The
#: console mirrors that rule instead of letting a chat turn bypass it.
APPROVAL_REQUIRED_COMMANDS = {
    "cluster.kubernetes.version.upgrade",
    "cluster.kubernetes.version.rollback",
}


class ChatToolError(Exception):
    """A tool that could not run; ``code`` is stable for the transcript."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ChatTool:
    name: str
    kind: str
    identifier: str
    description: str
    schema: dict
    handler: Callable[[dict], dict]


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _provider_name(raw: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in raw)
    return cleaned[:64]


# --------------------------------------------------------------------------
# Platform actions
# --------------------------------------------------------------------------


def _node_payload(node: AgentNode) -> dict:
    return {
        "id": str(node.id),
        "name": node.name,
        "node_type": node.node_type,
        "status": node.status,
        "cluster_name": node.cluster_name,
        "agent_version": node.agent_version,
        "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
    }


def _list_nodes(thread: AgentChatThread, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 20)
    nodes = (
        AgentNode.objects.filter(tenant=thread.tenant, project=thread.project)
        .order_by("name")[:limit]
    )
    items = [_node_payload(node) for node in nodes]
    return {"ok": True, "count": len(items), "nodes": items}


def _list_deployments(thread: AgentChatThread, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 10)
    rows = ClusterDeployment.objects.filter(
        tenant=thread.tenant, project=thread.project
    ).order_by("-created_at")[:limit]
    return {
        "ok": True,
        "count": len(rows),
        "deployments": [
            {
                "id": str(row.id),
                "cluster_name": row.cluster_name,
                "status": row.status,
                "kubernetes_version": row.kubernetes_version,
                "eph_node": row.eph_node.name,
                "target_node": row.target_node.name if row.target_node else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }


def _list_commands(thread: AgentChatThread, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 10)
    rows = AgentCommand.objects.filter(
        tenant=thread.tenant, project=thread.project
    ).select_related("node")[:limit]
    return {
        "ok": True,
        "count": len(rows),
        "commands": [
            {
                "id": str(row.id),
                "node": row.node.name,
                "command_type": row.command_type,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "error": row.error,
            }
            for row in rows
        ],
    }


def _list_registry_images(thread: AgentChatThread, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 20)
    rows = RegistryImage.objects.filter(
        tenant=thread.tenant, project=thread.project
    ).order_by("name")[:limit]
    return {
        "ok": True,
        "count": len(rows),
        "images": [
            {
                "name": row.name,
                "digest": row.digest,
                "status": row.status,
                "stage": row.stage,
                "scan_status": row.scan_status,
                "category": row.category,
                "signed": bool(getattr(row, "signature_verified_at", None)),
            }
            for row in rows
        ],
    }


def _list_scheduled_jobs(thread: AgentChatThread, arguments: dict) -> dict:
    rows = ScheduledChatJob.objects.filter(tenant=thread.tenant, project=thread.project)[:20]
    return {
        "ok": True,
        "count": len(rows),
        "jobs": [
            {
                "id": str(row.id),
                "name": row.name,
                "cadence": row.cadence,
                "hour": row.hour,
                "weekday": row.weekday,
                "enabled": row.enabled,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
                "last_error_code": row.last_error_code,
            }
            for row in rows
        ],
    }


def _create_command(thread: AgentChatThread, arguments: dict) -> dict:
    node_id = str(arguments.get("node_id") or "").strip()
    command_type = str(arguments.get("command_type") or "").strip()
    payload = arguments.get("payload") or {}
    if not command_type:
        raise ChatToolError("INVALID_TOOL_ARGUMENTS", "command_type is required")
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 65536:
        raise ChatToolError("INVALID_TOOL_ARGUMENTS", "payload must be a JSON object under 64 KiB")
    node = AgentNode.objects.filter(
        tenant=thread.tenant, project=thread.project, pk=node_id
    ).first()
    if node is None:
        raise ChatToolError("NODE_NOT_FOUND", "No enrolled node with that id in this project.")
    fingerprint = f"{thread.id}:{command_type}:{node.id}:{json.dumps(payload, sort_keys=True)}"
    idempotency_key = f"chat:{thread.id}:{uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)}"
    status = (
        AgentCommand.Status.WAITING_APPROVAL
        if command_type in APPROVAL_REQUIRED_COMMANDS
        else AgentCommand.Status.QUEUED
    )
    command, created = AgentCommand.objects.get_or_create(
        tenant=thread.tenant,
        idempotency_key=idempotency_key,
        defaults={
            "project": thread.project,
            "node": node,
            "requested_by": thread.created_by,
            "command_type": command_type,
            "payload": payload,
            "status": status,
            "plan_hash": uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex,
        },
    )
    return {
        "ok": True,
        "created": created,
        "command": {
            "id": str(command.id),
            "node": node.name,
            "command_type": command.command_type,
            "status": command.status,
            "requires_approval": command.status == AgentCommand.Status.WAITING_APPROVAL,
            "idempotency_key": command.idempotency_key,
        },
    }


#: A local runner is a machine that hosts the console; it may only be running
#: probes, so the turn waits rather than leaving work queued forever.
LOCAL_COMMAND_TIMEOUT_SECONDS = 25
LOCAL_RUNNER_STALE_SECONDS = 120


def _local_runner(thread: AgentChatThread) -> AgentNode:
    """The freshest online local runner enrolled in this project."""

    node = (
        AgentNode.objects.filter(
            tenant=thread.tenant,
            project=thread.project,
            node_type=AgentNode.NodeType.LOCAL_RUNNER,
            status=AgentNode.Status.ONLINE,
        )
        .order_by("-last_seen_at")
        .first()
    )
    if node is None:
        raise ChatToolError(
            "LOCAL_RUNNER_OFFLINE",
            "No local runner is online for this project. Start the local agent on your "
            "machine and it will appear here.",
        )
    seen = node.last_seen_at
    if seen is None or (timezone.now() - seen).total_seconds() > LOCAL_RUNNER_STALE_SECONDS:
        raise ChatToolError(
            "LOCAL_RUNNER_OFFLINE",
            f"Local runner {node.name} has not sent a heartbeat recently.",
        )
    return node


def _run_local(thread: AgentChatThread, command_type: str, payload: dict) -> dict:
    """Queue one read-only probe on the local runner and wait for its result."""

    node = _local_runner(thread)
    digest = hashlib.sha256(
        json.dumps({"command_type": command_type, "payload": payload}, sort_keys=True).encode()
    ).hexdigest()
    command, created = AgentCommand.objects.get_or_create(
        idempotency_key=f"chat:{thread.id}:{uuid.uuid5(uuid.NAMESPACE_URL, command_type + digest)}",
        defaults={
            "tenant": thread.tenant,
            "project": thread.project,
            "node": node,
            "requested_by": thread.created_by,
            "command_type": command_type,
            "payload": payload,
            "status": AgentCommand.Status.QUEUED,
            "plan_hash": digest,
        },
    )
    deadline = time.monotonic() + LOCAL_COMMAND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        command.refresh_from_db()
        if command.status == AgentCommand.Status.SUCCEEDED:
            return {
                "ok": True,
                "created": created,
                "runner": node.name,
                "command_type": command_type,
                "result": command.result,
            }
        if command.status in {
            AgentCommand.Status.FAILED,
            AgentCommand.Status.CANCELLED,
            AgentCommand.Status.EXPIRED,
        }:
            raise ChatToolError(
                "LOCAL_COMMAND_FAILED",
                f"{node.name} rejected {command_type}: {json.dumps(command.error or {})[:200]}",
            )
        time.sleep(0.4)
    raise ChatToolError(
        "LOCAL_COMMAND_TIMEOUT",
        f"{node.name} did not answer {command_type} within {LOCAL_COMMAND_TIMEOUT_SECONDS}s.",
    )


def _local_system_info(thread: AgentChatThread, arguments: dict) -> dict:
    return _run_local(thread, "local.system_info", {})


def _local_disk_usage(thread: AgentChatThread, arguments: dict) -> dict:
    path = arguments.get("path") or "/"
    return _run_local(thread, "local.disk_usage", {"path": str(path)})


LOCAL_TOOLS: tuple[ChatTool, ...] = (
    ChatTool(
        name="local_system_info",
        kind="local",
        identifier="local.system_info",
        description=(
            "Ask the local runner on this machine for host facts: hostname, operating system, "
            "kernel, CPU count, load average and uptime."
        ),
        schema=_schema({}),
        handler=_local_system_info,
    ),
    ChatTool(
        name="local_disk_usage",
        kind="local",
        identifier="local.disk_usage",
        description="Ask the local runner for free and total disk space on a path.",
        schema=_schema({"path": {"type": "string", "description": "Absolute path, default /"}}),
        handler=_local_disk_usage,
    ),
)


PLATFORM_TOOLS: tuple[ChatTool, ...] = (
    ChatTool(
        name="stack_list_nodes",
        kind="platform",
        identifier="platform.list_nodes",
        description=(
            "List the enrolled agent nodes of this project with type, status and last seen "
            "time. Use it before recommending any deployment step."
        ),
        schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        handler=_list_nodes,
    ),
    ChatTool(
        name="stack_list_deployments",
        kind="platform",
        identifier="platform.list_deployments",
        description=(
            "List cluster deployments of this project with their live control-plane status "
            "(preflight, planning, plan ready, provisioning, workers ready, target agent)."
        ),
        schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
        handler=_list_deployments,
    ),
    ChatTool(
        name="stack_list_commands",
        kind="platform",
        identifier="platform.list_commands",
        description="List the most recent agent commands with status and error payload.",
        schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
        handler=_list_commands,
    ),
    ChatTool(
        name="stack_list_registry_images",
        kind="platform",
        identifier="platform.list_registry_images",
        description=(
            "List the registry images of this project with digest, stage, scan and signature "
            "state. Deployments may only reference a digest that is ready here."
        ),
        schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        handler=_list_registry_images,
    ),
    ChatTool(
        name="stack_list_scheduled_jobs",
        kind="platform",
        identifier="platform.list_scheduled_jobs",
        description="List the scheduled turns of this project with cadence and last outcome.",
        schema=_schema({}),
        handler=_list_scheduled_jobs,
    ),
    ChatTool(
        name="stack_create_command",
        kind="platform",
        identifier="platform.create_command",
        description=(
            "File a real AgentCommand against an enrolled node. Commands that change a "
            "cluster land in waiting_approval and need a human approval before they run; "
            "everything else is queued for the node."
        ),
        schema=_schema(
            {
                "node_id": {"type": "string", "description": "Enrolled node id"},
                "command_type": {
                    "type": "string",
                    "description": "Command name, e.g. cluster.kubernetes.version.upgrade",
                },
                "payload": {"type": "object", "description": "Command payload, at most 64 KiB"},
            },
            ["node_id", "command_type"],
        ),
        handler=_create_command,
    ),
)

# --------------------------------------------------------------------------
# Plugin capabilities
# --------------------------------------------------------------------------


def _capability_schema(definition: dict) -> dict:
    schema = definition.get("input_schema") or {"type": "object", "properties": {}, "required": []}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return _schema({})
    return schema


def _plugin_tools(thread: AgentChatThread, context: dict) -> list[ChatTool]:
    """Capabilities the member scoped in: by capability (skill) or whole plugin."""
    installations = list(
        AgentPluginInstallation.objects.filter(
            tenant=thread.tenant, project=thread.project
        ).select_related("plugin")
    )
    selected_plugin_ids = set(context.get("plugins") or [])
    selected_skills = set(context.get("skills") or [])
    tools: list[ChatTool] = []
    seen_names: set[str] = set()
    for installation in installations:
        if installation.status != AgentPluginInstallation.Status.INSTALLED:
            continue
        plugin: AgentPluginDefinition = installation.plugin
        if not plugin.enabled:
            continue
        whole_plugin = str(installation.id) in selected_plugin_ids
        granted = [
            capability
            for capability in (installation.granted_capabilities or [])
            if isinstance(capability, str)
        ]
        for definition in plugin.capabilities or []:
            name = definition.get("name") if isinstance(definition, dict) else None
            if not isinstance(name, str) or name not in granted:
                continue
            if not whole_plugin and name not in selected_skills:
                continue
            tool_name = _provider_name(f"{plugin.plugin_key}__{name}")
            if tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            tools.append(
                ChatTool(
                    name=tool_name,
                    kind="plugin",
                    identifier=name,
                    description=str(definition.get("description") or f"Execute {name}")[:1024],
                    schema=_capability_schema(definition),
                    handler=_capability_handler(thread, name),
                )
            )
    return tools


def _capability_handler(thread: AgentChatThread, capability: str):
    def handler(arguments: dict) -> dict:
        try:
            result = invoke_capability(
                str(thread.tenant_id),
                str(thread.project_id),
                capability,
                arguments,
                approved=False,
            )
        except CapabilityResolutionError as exc:
            raise ChatToolError(exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - adapters raise their own taxonomy
            code = getattr(exc, "code", "CAPABILITY_INVOCATION_FAILED")
            raise ChatToolError(str(code), str(exc)) from exc
        return {"ok": True, "capability": capability, "result": result}

    return handler


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def build_tools(thread: AgentChatThread, context: dict) -> list[ChatTool]:
    tools = [_bind_platform(tool, thread) for tool in PLATFORM_TOOLS]
    if thread.execution_target == "local":
        tools += [_bind_platform(tool, thread) for tool in LOCAL_TOOLS]
    tools += _plugin_tools(thread, context)
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ChatToolError("TOOL_NAME_COLLISION", "Two tools resolved to the same function name.")
    return tools


def _bind_platform(tool: ChatTool, thread: AgentChatThread) -> ChatTool:
    """Platform handlers take the thread (tenant scope); the loop calls one arg."""
    return ChatTool(
        name=tool.name,
        kind=tool.kind,
        identifier=tool.identifier,
        description=tool.description,
        schema=tool.schema,
        handler=lambda arguments: tool.handler(thread, arguments),
    )


def to_openai_tools(tools: list[ChatTool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": f"[{tool.kind}] {tool.identifier}: {tool.description}",
                "parameters": tool.schema,
            },
        }
        for tool in tools
    ]


def run_tool(tool: ChatTool, arguments: dict) -> dict:
    """Execute one call and bound the payload that goes back to the model."""
    try:
        payload = tool.handler(arguments)
    except ChatToolError as exc:
        return {"ok": False, "code": exc.code, "error": exc.message}
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return {
            "ok": payload.get("ok", True),
            "truncated": True,
            "note": f"Result exceeded {MAX_TOOL_RESULT_CHARS} characters and was truncated.",
            "preview": text[:MAX_TOOL_RESULT_CHARS],
        }
    return payload
