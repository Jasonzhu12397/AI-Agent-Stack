# ruff: noqa: I001
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from core.http_connector import _resolve_auth_headers
from core.tool_router import (
    FAULT_CLIENT_ERROR,
    FAULT_INVALID_RESPONSE,
    FAULT_SERVER_ERROR,
    FAULT_TIMEOUT,
    FAULT_UNREACHABLE,
    FAULT_UPSTREAM_ERROR,
    ToolExecutionError,
)


DEFAULT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "ai-agent-stack", "version": "1.0.0"}
MAX_RESPONSE_BYTES = 1048576
MAX_TOOL_PAGES = 10
MAX_TOOL_COUNT = 256
MAX_CONTENT_ITEMS = 100
MAX_TEXT_ITEM_CHARS = 65536
JSONRPC_VERSION = "2.0"


def _reject_json_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _validate_endpoint(url):
    if not isinstance(url, str) or len(url) > 500:
        raise ToolExecutionError("MCP endpoint URL is invalid.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolExecutionError("MCP endpoint URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or re.search(r"%2e", parsed.path, re.IGNORECASE)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ToolExecutionError("MCP connectors require an HTTPS origin on port 443.")
    host = parsed.hostname.lower().rstrip(".")
    allowed_hosts = set(getattr(settings, "PLUGIN_HTTP_ALLOWED_HOSTS", []))
    if host not in allowed_hosts:
        raise ToolExecutionError("MCP connector host is not in the platform allowlist.")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolExecutionError("MCP connector host could not be resolved.") from exc
    resolved = {item[4][0] for item in addresses}
    if not resolved:
        raise ToolExecutionError("MCP connector host has no resolved addresses.")
    for address in resolved:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ToolExecutionError("MCP connector resolved an invalid address.") from exc
        if not parsed_address.is_global:
            raise ToolExecutionError("MCP connector host resolves to a non-public address.")
    return url


def _load_json(payload):
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ToolExecutionError(
            "MCP response is not valid UTF-8 JSON.",
            code=FAULT_INVALID_RESPONSE,
        ) from exc


def _content_type(response):
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _parse_sse_messages(payload):
    """Parse an SSE body into a list of JSON ``data`` payloads."""

    messages = []
    data_lines = []
    for raw_line in payload.decode("utf-8", errors="strict").split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                messages.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data_lines.append(line[5:].lstrip())
    if data_lines:
        messages.append("\n".join(data_lines))
    parsed = []
    for message in messages:
        if not message or message == "[DONE]":
            continue
        parsed.append(_load_json(message.encode("utf-8")))
    return parsed


def _read_body(response):
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ToolExecutionError("MCP response exceeds 1 MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


class McpClient:
    """Minimal MCP client for the streamable HTTP transport.

    The session performs the ``initialize`` handshake, then exposes
    ``tools/list`` and ``tools/call``. Every call is a JSON-RPC 2.0 request
    answered either by a JSON body or by an SSE stream that carries the
    matching response frame.
    """

    def __init__(self, endpoint, headers, timeout_seconds):
        self.endpoint = endpoint
        self.headers = dict(headers)
        self.timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(timeout_seconds, 10),
        )
        self.session_id = ""
        self.protocol_version = ""
        self._next_id = 0
        self._client = None

    def __enter__(self):
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._client is not None:
            self._client.close()
        self._client = None
        return False

    def _request_headers(self):
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload):
        if self._client is None:
            raise ToolExecutionError("MCP client session is not open.")
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ToolExecutionError("MCP request exceeds 1 MiB.")
        try:
            with self._client.stream(
                "POST",
                self.endpoint,
                content=body,
                headers=self._request_headers(),
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise ToolExecutionError(
                        "MCP redirects are not allowed.",
                        code=FAULT_UNREACHABLE,
                    )
                if 400 <= status < 500:
                    raise ToolExecutionError(
                        "MCP endpoint returned a 4xx status.",
                        code=FAULT_CLIENT_ERROR,
                    )
                if 500 <= status < 600:
                    raise ToolExecutionError(
                        "MCP endpoint returned a 5xx status.",
                        code=FAULT_SERVER_ERROR,
                    )
                if not 200 <= status < 300:
                    raise ToolExecutionError("MCP endpoint returned a non-success status.")
                content_type = _content_type(response)
                if not content_type:
                    raise ToolExecutionError(
                        "MCP response content type is missing.",
                        code=FAULT_INVALID_RESPONSE,
                    )
                if not (
                    content_type == "application/json"
                    or content_type.endswith("+json")
                    or content_type == "text/event-stream"
                ):
                    raise ToolExecutionError(
                        "MCP response content type is not supported.",
                        code=FAULT_INVALID_RESPONSE,
                    )
                session_id = response.headers.get("Mcp-Session-Id", "")
                payload_bytes = _read_body(response)
        except ToolExecutionError:
            raise
        # ConnectTimeout is classified UNREACHABLE: the handshake never completed.
        # Remaining TimeoutException subclasses mean a connected upstream stalled.
        except httpx.ConnectTimeout as exc:
            raise ToolExecutionError(
                "MCP client could not establish a connection.",
                code=FAULT_UNREACHABLE,
            ) from exc
        except (httpx.ConnectError, httpx.TooManyRedirects) as exc:
            raise ToolExecutionError(
                "MCP endpoint is unreachable.",
                code=FAULT_UNREACHABLE,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ToolExecutionError(
                "MCP request timed out.",
                code=FAULT_TIMEOUT,
            ) from exc
        except httpx.HTTPError:
            raise ToolExecutionError(
                "MCP request failed.",
                code=FAULT_UNREACHABLE,
            ) from None
        return content_type, session_id, payload_bytes

    def _send(self, method, params=None, *, expect_response=True):
        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        if expect_response:
            payload["id"] = request_id
        content_type, session_id, body = self._post(payload)
        if session_id:
            self.session_id = session_id[:128]
        if not expect_response:
            return None
        if content_type == "text/event-stream":
            messages = _parse_sse_messages(body)
        else:
            messages = [_load_json(body)]
        for message in messages:
            if not isinstance(message, dict):
                raise ToolExecutionError(
                    "MCP response frame is not a JSON object.",
                    code=FAULT_INVALID_RESPONSE,
                )
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error")
                if not isinstance(error, dict):
                    raise ToolExecutionError(
                        "MCP server returned an invalid error object.",
                        code=FAULT_UPSTREAM_ERROR,
                    )
                code = error.get("code")
                text = str(error.get("message", ""))[:200]
                raise ToolExecutionError(
                    f"MCP server error ({code}): {text}",
                    code=FAULT_UPSTREAM_ERROR,
                )
            if "result" not in message:
                raise ToolExecutionError(
                    "MCP server response is missing a result.",
                    code=FAULT_INVALID_RESPONSE,
                )
            return message["result"]
        raise ToolExecutionError(
            "MCP server did not answer the request.",
            code=FAULT_INVALID_RESPONSE,
        )

    def initialize(self):
        result = self._send(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
        )
        if not isinstance(result, dict):
            raise ToolExecutionError("MCP initialize result is invalid.")
        protocol_version = result.get("protocolVersion")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise ToolExecutionError("MCP server did not negotiate a protocol version.")
        self.protocol_version = protocol_version[:32]
        self._send("notifications/initialized", None, expect_response=False)
        return result

    def list_tools(self):
        tools = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self._send("tools/list", params)
            if not isinstance(result, dict):
                raise ToolExecutionError("MCP tools/list result is invalid.")
            page = result.get("tools")
            if not isinstance(page, list):
                raise ToolExecutionError("MCP tools/list did not return a tool list.")
            for entry in page:
                if not isinstance(entry, dict):
                    raise ToolExecutionError("MCP tool descriptor is invalid.")
                name = entry.get("name")
                if not isinstance(name, str) or not name or len(name) > 100:
                    raise ToolExecutionError("MCP tool name is invalid.")
                schema = entry.get("inputSchema", {})
                if not isinstance(schema, dict):
                    raise ToolExecutionError("MCP tool input schema is invalid.")
                tools.append(
                    {
                        "name": name,
                        "description": str(entry.get("description", ""))[:2000],
                        "input_schema": schema,
                    }
                )
                if len(tools) > MAX_TOOL_COUNT:
                    raise ToolExecutionError("MCP server exposed too many tools.")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tools
            if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 256:
                raise ToolExecutionError("MCP tools/list cursor is invalid.")
            cursor = next_cursor
        raise ToolExecutionError("MCP tools/list pagination did not terminate.")

    def call_tool(self, name, arguments):
        result = self._send(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return _normalize_tool_result(result)


def _normalize_tool_result(result):
    if not isinstance(result, dict):
        raise ToolExecutionError("MCP tools/call result is invalid.")
    content = result.get("content", [])
    if not isinstance(content, list) or len(content) > MAX_CONTENT_ITEMS:
        raise ToolExecutionError("MCP tool content is invalid.")
    normalized = []
    for item in content:
        if not isinstance(item, dict):
            raise ToolExecutionError("MCP tool content item is invalid.")
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT_ITEM_CHARS:
                raise ToolExecutionError("MCP tool text content is invalid.")
            normalized.append({"type": "text", "text": text})
        elif item_type in {"image", "audio", "resource"}:
            normalized.append({"type": item_type})
        else:
            raise ToolExecutionError("MCP tool content type is not supported.")
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        raise ToolExecutionError("MCP tool error flag is invalid.")
    structured = result.get("structuredContent")
    if structured is not None and not isinstance(structured, dict):
        raise ToolExecutionError("MCP structured content is invalid.")
    payload = {
        "content": normalized,
        "isError": is_error,
    }
    if structured is not None:
        payload["structuredContent"] = structured
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ToolExecutionError("MCP tool result exceeds 1 MiB.")
    return payload


def _mcp_config(context):
    transport = context.plugin_manifest.get("mcp")
    if not isinstance(transport, dict):
        raise ToolExecutionError("MCP transport manifest is missing.")
    endpoint = _validate_endpoint(transport.get("endpoint_url"))
    protocol_version = transport.get("protocol_version", DEFAULT_PROTOCOL_VERSION)
    if not isinstance(protocol_version, str) or len(protocol_version) > 32:
        raise ToolExecutionError("MCP protocol version is invalid.")
    tool_map = transport.get("tool_map", {})
    if not isinstance(tool_map, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in tool_map.items()
    ):
        raise ToolExecutionError("MCP tool mapping is invalid.")
    timeout_seconds = min(max(int(context.plugin_manifest.get("timeout_seconds", 60)), 1), 300)
    return endpoint, tool_map, timeout_seconds


def execute_mcp_operation(arguments, context, tool):
    if not isinstance(arguments, dict):
        raise ToolExecutionError("MCP tool arguments must be a JSON object.")
    endpoint, tool_map, timeout_seconds = _mcp_config(context)
    remote_name = tool_map.get(tool.name, tool.name)
    if not isinstance(remote_name, str) or len(remote_name) > 100:
        raise ToolExecutionError("MCP tool mapping produced an invalid tool name.")
    headers = _resolve_auth_headers(context)
    timeout_seconds = min(max(int(getattr(tool, "timeout_seconds", timeout_seconds) or 1), 1), 300)
    with McpClient(endpoint, headers, timeout_seconds) as client:
        client.initialize()
        return client.call_tool(remote_name, arguments)


def discover_mcp_tools(context):
    """Return the tool descriptors that the configured MCP server exposes."""

    endpoint, _, timeout_seconds = _mcp_config(context)
    headers = _resolve_auth_headers(context)
    with McpClient(endpoint, headers, timeout_seconds) as client:
        client.initialize()
        return client.list_tools()


def register_mcp_tool(router, tool):
    if tool.plugin.connector_type != "mcp":
        return False
    if router.has_handler(
        tool.plugin.plugin_key,
        tool.name,
        plugin_version=tool.plugin.version,
    ):
        return False

    def handler(arguments, context):
        return execute_mcp_operation(arguments, context, tool)

    router.register(
        tool.plugin.plugin_key,
        tool.name,
        handler,
        plugin_version=tool.plugin.version,
    )
    return True
