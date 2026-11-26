"""MCP server mode: expose project-scoped platform tools over JSON-RPC.

The server is intentionally a thin adapter over existing API semantics. It
does not invent a second permission model: every call resolves the tenant and
project from the URL, authenticates the same user as the console, and reuses
the project membership checks and capability router. High-risk connector
capabilities still stop at the approval gate.
"""

from __future__ import annotations

import json

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import agent_plugin_manifest
from core.agent_capability_router import CapabilityResolutionError, invoke_capability
from core.audit import record_audit_event
from core.models import (
    AgentPluginInstallation,
    Task,
)
from core.serializers import TaskSerializer
from core.task_views import (
    SUPPORTED_TASK_TYPES,
    accessible_project,
    accessible_tenant,
    can_create_task,
)
from core.tool_router import ToolApprovalRequired, ToolExecutionError

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ai-agent-stack", "version": "1.0.0"}
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

MAX_TASKS_PER_PAGE = 50
MAX_TOOL_RESULT_CHARS = 65536


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "platform_list_tasks",
        "description": "List tasks in this project, newest first.",
        "inputSchema": _schema(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TASKS_PER_PAGE},
                "status": {
                    "type": "string",
                    "enum": [choice for choice, _ in Task.Status.choices],
                },
            }
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "platform_get_task",
        "description": "Read one task by id in this project.",
        "inputSchema": _schema({"task_id": {"type": "string"}}, ("task_id",)),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "platform_create_task",
        "description": (
            "Create a task in this project. Requires editor permission and a unique "
            "idempotency key."
        ),
        "inputSchema": _schema(
            {
                "task_type": {"type": "string", "enum": sorted(SUPPORTED_TASK_TYPES)},
                "payload": {"type": "object"},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            ("task_type", "idempotency_key"),
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "platform_list_capabilities",
        "description": "List the connector capabilities installed in this project.",
        "inputSchema": _schema({}, ()),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "platform_invoke_capability",
        "description": (
            "Invoke one installed connector capability. High-risk capabilities "
            "require platform approval."
        ),
        "inputSchema": _schema(
            {
                "capability": {"type": "string", "minLength": 1, "maxLength": 64},
                "arguments": {"type": "object"},
            },
            ("capability",),
        ),
        "annotations": {"readOnlyHint": False},
    },
]


class McpServerError(Exception):
    def __init__(self, code, message, *, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _tool_result(payload, *, is_error=False):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        raise McpServerError(INTERNAL_ERROR, "Tool result exceeds 64 KiB.")
    return {
        "content": [{"type": "text", "text": encoded}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


def _task_payload(task):
    return dict(TaskSerializer(task).data)


def _list_tasks(request, tenant, project, arguments):
    limit = arguments.get("limit", 20)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_TASKS_PER_PAGE
    ):
        raise McpServerError(INVALID_PARAMS, "limit must be an integer between 1 and 50.")
    status_value = arguments.get("status")
    queryset = Task.objects.filter(tenant=tenant, project=project).order_by("-created_at")
    if status_value is not None:
        if status_value not in set(Task.Status.values):
            raise McpServerError(INVALID_PARAMS, "status is not a known task status.")
        queryset = queryset.filter(status=status_value)
    return {"tasks": [_task_payload(task) for task in queryset[:limit]], "limit": limit}


def _get_task(request, tenant, project, arguments):
    task_id = arguments.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise McpServerError(INVALID_PARAMS, "task_id is required.")
    task = Task.objects.filter(tenant=tenant, project=project, pk=task_id).first()
    if task is None:
        raise McpServerError(INVALID_PARAMS, "Task not found in this project.")
    return {"task": _task_payload(task)}


def _create_task(request, tenant, project, arguments):
    if not can_create_task(request, tenant, project):
        raise McpServerError(INVALID_PARAMS, "Project editor permission required.")
    task_type = arguments.get("task_type")
    payload = arguments.get("payload", {})
    idempotency_key = arguments.get("idempotency_key")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise McpServerError(INVALID_PARAMS, "Unsupported task_type.")
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 65536:
        raise McpServerError(INVALID_PARAMS, "payload must be a JSON object no larger than 64 KiB.")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
        raise McpServerError(
            INVALID_PARAMS,
            "idempotency_key is required and must be at most 128 chars.",
        )
    try:
        with transaction.atomic():
            task, created = Task.objects.get_or_create(
                tenant=tenant,
                idempotency_key=idempotency_key,
                defaults={
                    "project": project,
                    "created_by": request.user,
                    "task_type": task_type,
                    "payload": payload,
                    "available_at": timezone.now(),
                },
            )
    except IntegrityError:
        task = Task.objects.get(tenant=tenant, idempotency_key=idempotency_key)
        created = False
    if not created and (
        task.project_id != project.id or task.task_type != task_type or task.payload != payload
    ):
        raise McpServerError(
            INVALID_PARAMS,
            "idempotency_key was already used with a different request.",
        )
    if created:
        record_audit_event(
            request,
            tenant,
            "task.created",
            "task",
            task.id,
            project=project,
            metadata={"source": "mcp_server"},
        )
    return {"task": _task_payload(task), "created": created}


def _list_capabilities(request, tenant, project, arguments):
    installations = AgentPluginInstallation.objects.filter(
        tenant=tenant,
        project=project,
        status=AgentPluginInstallation.Status.INSTALLED,
    ).select_related("plugin")
    capabilities = []
    for installation in installations:
        plugin = installation.plugin
        if not plugin.enabled:
            continue
        for definition in agent_plugin_manifest.declared_capabilities(plugin):
            name = definition.get("name")
            if not name:
                continue
            capabilities.append(
                {
                    "capability": name,
                    "plugin_key": plugin.plugin_key,
                    "plugin_version": installation.plugin_version,
                    "risk_level": definition.get("risk_level", "low"),
                    "input_schema": definition.get("input_schema", {}),
                }
            )
    capabilities.sort(key=lambda item: (item["capability"], item["plugin_key"]))
    return {"capabilities": capabilities}


def _invoke_capability(request, tenant, project, arguments):
    capability = arguments.get("capability")
    if not isinstance(capability, str) or not capability or len(capability) > 64:
        raise McpServerError(INVALID_PARAMS, "capability is required.")
    invocation_arguments = arguments.get("arguments", {})
    if not isinstance(invocation_arguments, dict):
        raise McpServerError(INVALID_PARAMS, "arguments must be an object.")
    try:
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            capability,
            invocation_arguments,
            approved=False,
        )
    except ToolApprovalRequired as exc:
        raise McpServerError(INVALID_PARAMS, str(exc), data={"code": "APPROVAL_REQUIRED"}) from exc
    except CapabilityResolutionError as exc:
        raise McpServerError(INVALID_PARAMS, str(exc), data={"code": exc.code}) from exc
    except ToolExecutionError as exc:
        raise McpServerError(INVALID_PARAMS, str(exc)) from exc
    record_audit_event(
        request,
        tenant,
        "mcp_server.capability_invoked",
        "agent_plugin",
        capability,
        project=project,
        metadata={"source": "mcp_server"},
    )
    return {"capability": capability, "result": result}


TOOL_HANDLERS = {
    "platform_list_tasks": _list_tasks,
    "platform_get_task": _get_task,
    "platform_create_task": _create_task,
    "platform_list_capabilities": _list_capabilities,
    "platform_invoke_capability": _invoke_capability,
}


def _initialize(params):
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


def _tools_list(params):
    return {"tools": TOOLS}


def _tools_call(request, tenant, project, params):
    if not isinstance(params, dict):
        raise McpServerError(INVALID_PARAMS, "tools/call params must be an object.")
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_HANDLERS:
        raise McpServerError(INVALID_PARAMS, "Unknown tool.")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise McpServerError(INVALID_PARAMS, "Tool arguments must be an object.")
    payload = TOOL_HANDLERS[name](request, tenant, project, arguments)
    return _tool_result(payload)


def dispatch(request, tenant, project, message):
    if not isinstance(message, dict):
        raise McpServerError(INVALID_REQUEST, "Request must be a JSON object.")
    if message.get("jsonrpc") != "2.0":
        raise McpServerError(INVALID_REQUEST, "jsonrpc must be '2.0'.")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise McpServerError(INVALID_REQUEST, "method is required.")
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise McpServerError(INVALID_PARAMS, "params must be an object.")
    if method == "initialize":
        return _initialize(params)
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return _tools_list(params)
    if method == "tools/call":
        return _tools_call(request, tenant, project, params)
    raise McpServerError(METHOD_NOT_FOUND, "Method not found.")


def _error_response(request_id, error):
    body = {"jsonrpc": "2.0", "error": {"code": error.code, "message": error.message}}
    if error.data is not None:
        body["error"]["data"] = error.data
    body["id"] = request_id
    return body


def _read_payload(request):
    raw = request.body or b""
    if not raw:
        raise McpServerError(INVALID_REQUEST, "An empty body is not a JSON-RPC message.")
    if len(raw) > 1024 * 1024:
        raise McpServerError(INVALID_REQUEST, "JSON-RPC body exceeds 1 MiB.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise McpServerError(PARSE_ERROR, "Invalid JSON.") from exc


class McpServerView(APIView):
    """HTTP JSON-RPC endpoint for MCP clients bound to one project."""

    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id):
        tenant = accessible_tenant(request, tenant_id)
        project = accessible_project(request, tenant, project_id)
        try:
            payload = _read_payload(request)
        except McpServerError as exc:
            return Response(_error_response(None, exc), status=status.HTTP_200_OK)
        if isinstance(payload, list):
            return Response(
                _error_response(
                    None,
                    McpServerError(INVALID_REQUEST, "Batch requests are not supported."),
                ),
                status=status.HTTP_200_OK,
            )
        request_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            result = dispatch(request, tenant, project, payload)
        except McpServerError as exc:
            return Response(_error_response(request_id, exc), status=status.HTTP_200_OK)
        except Exception:
            # Internal failures are not echoed: the message could contain upstream
            # detail or a credential reference.
            return Response(
                _error_response(request_id, McpServerError(INTERNAL_ERROR, "Internal error.")),
                status=status.HTTP_200_OK,
            )
        if result is None:
            return Response(status=status.HTTP_202_ACCEPTED)
        return Response(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            status=status.HTTP_200_OK,
        )
