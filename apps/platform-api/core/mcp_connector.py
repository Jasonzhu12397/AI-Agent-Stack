# ruff: noqa: I001
import ipaddress
import json
import os
import re
import select
import shutil
import socket
import subprocess
import threading
import time
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
MAX_STDIO_ARGUMENTS = 64
MAX_STDIO_ARGUMENT_CHARS = 1000
MAX_STDERR_CAPTURE = 8192
JSONRPC_VERSION = "2.0"


def _reject_json_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _validate_endpoint(url, require_allowlist=True):
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
    if require_allowlist:
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



class McpStdioClient(McpClient):
    """MCP client for a locally executed stdio server.

    The server is launched without a shell, with a minimal environment, and
    receives one JSON-RPC message per line on stdin. Stdout is parsed as line
    delimited JSON. Stderr is drained continuously so a noisy server cannot
    block on a full pipe. The process is terminated when the client exits.
    """

    def __init__(self, command, arguments, env, timeout_seconds, workdir=None):
        # Deliberately do not call McpClient.__init__: there is no HTTP endpoint
        # or httpx client for this transport.
        self.command = command
        self.arguments = list(arguments)
        self.env = dict(env)
        self.timeout_seconds = timeout_seconds
        self.workdir = workdir
        self.protocol_version = ""
        self._next_id = 0
        self._process = None
        self._stderr_lines = []
        self._stderr_thread = None

    def __enter__(self):
        try:
            self._process = subprocess.Popen(
                [self.command, *self.arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.workdir,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise ToolExecutionError(
                "MCP stdio server could not be started.",
                code=FAULT_UNREACHABLE,
            ) from exc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="mcp-stdio-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        process = self._process
        self._process = None
        if process is None:
            return False
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        return False

    def _drain_stderr(self):
        stream = self._process.stderr if self._process else None
        if stream is None:
            return
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                self._stderr_lines.append(text)
                if sum(len(item) for item in self._stderr_lines) > MAX_STDERR_CAPTURE:
                    self._stderr_lines = self._stderr_lines[-10:]
        except (OSError, ValueError):
            return

    def _stderr_tail(self):
        text = " ".join(self._stderr_lines)[-500:]
        return " ".join(text.split())

    def _write(self, payload):
        process = self._process
        if process is None or process.stdin is None:
            raise ToolExecutionError("MCP stdio server is not running.")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ToolExecutionError("MCP request exceeds 1 MiB.")
        try:
            process.stdin.write(encoded + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ToolExecutionError(
                "MCP stdio server closed its input.",
                code=FAULT_UNREACHABLE,
            ) from exc

    def _read_message(self, request_id, deadline):
        process = self._process
        if process is None or process.stdout is None:
            raise ToolExecutionError("MCP stdio server is not running.")
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolExecutionError("MCP stdio request timed out.", code=FAULT_TIMEOUT)
            try:
                ready, _, _ = select.select([process.stdout], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise ToolExecutionError(
                    "MCP stdio stdout is unavailable.",
                    code=FAULT_UNREACHABLE,
                ) from exc
            if not ready:
                raise ToolExecutionError("MCP stdio request timed out.", code=FAULT_TIMEOUT)
            line = process.stdout.readline()
            if not line:
                detail = self._stderr_tail()
                raise ToolExecutionError(
                    "MCP stdio server exited before answering."
                    + (f" stderr: {detail}" if detail else ""),
                    code=FAULT_UNREACHABLE,
                )
            total += len(line)
            if total > MAX_RESPONSE_BYTES:
                raise ToolExecutionError("MCP stdio response exceeds 1 MiB.")
            if not line.strip():
                continue
            message = _load_json(line.strip())
            if not isinstance(message, dict):
                raise ToolExecutionError(
                    "MCP stdio response frame is not a JSON object.",
                    code=FAULT_INVALID_RESPONSE,
                )
            if message.get("id") != request_id:
                # Progress notifications and unrelated responses are ignored.
                continue
            if "error" in message:
                error = message.get("error")
                if not isinstance(error, dict):
                    raise ToolExecutionError(
                        "MCP server returned an invalid error object.",
                        code=FAULT_UPSTREAM_ERROR,
                    )
                text = str(error.get("message", ""))[:200]
                raise ToolExecutionError(
                    f"MCP server error ({error.get('code')}): {text}",
                    code=FAULT_UPSTREAM_ERROR,
                )
            if "result" not in message:
                raise ToolExecutionError(
                    "MCP server response is missing a result.",
                    code=FAULT_INVALID_RESPONSE,
                )
            return message["result"]

    def _send(self, method, params=None, *, expect_response=True):
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            payload["params"] = params
        if expect_response:
            payload["id"] = request_id
        deadline = time.monotonic() + self.timeout_seconds
        self._write(payload)
        if not expect_response:
            return None
        return self._read_message(request_id, deadline)



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


def _mcp_transport_config(context):
    transport = context.plugin_manifest.get("mcp")
    if not isinstance(transport, dict):
        raise ToolExecutionError("MCP transport manifest is missing.")
    tool_map = transport.get("tool_map", {})
    if not isinstance(tool_map, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in tool_map.items()
    ):
        raise ToolExecutionError("MCP tool mapping is invalid.")
    timeout_seconds = min(
        max(int(context.plugin_manifest.get("timeout_seconds", 60)), 1),
        300,
    )
    kind = transport.get("transport", "http")
    if kind in {"http", "streamable_http", "streamable-http"}:
        endpoint = _validate_endpoint(transport.get("endpoint_url"))
        protocol_version = transport.get("protocol_version", DEFAULT_PROTOCOL_VERSION)
        if not isinstance(protocol_version, str) or len(protocol_version) > 32:
            raise ToolExecutionError("MCP protocol version is invalid.")
        return {
            "kind": "http",
            "endpoint": endpoint,
            "protocol_version": protocol_version,
            "tool_map": tool_map,
            "timeout_seconds": timeout_seconds,
        }
    if kind == "stdio":
        return {
            "kind": "stdio",
            **_stdio_config(transport),
            "tool_map": tool_map,
            "timeout_seconds": timeout_seconds,
        }
    raise ToolExecutionError("MCP transport type is not supported.")


def _stdio_config(transport):
    command = transport.get("command")
    if not isinstance(command, str) or not command or len(command) > 200:
        raise ToolExecutionError("MCP stdio command is invalid.")
    if "\x00" in command or "\n" in command or "\r" in command:
        raise ToolExecutionError("MCP stdio command contains control characters.")
    arguments = transport.get("args", [])
    if not isinstance(arguments, list) or len(arguments) > MAX_STDIO_ARGUMENTS:
        raise ToolExecutionError("MCP stdio arguments must be a bounded list.")
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or len(argument) > MAX_STDIO_ARGUMENT_CHARS
            or "\x00" in argument
        ):
            raise ToolExecutionError("MCP stdio argument is invalid.")
    allowlist = getattr(settings, "MCP_STDIO_ALLOWED_COMMANDS", [])
    if not isinstance(allowlist, (list, tuple)) or not allowlist:
        raise ToolExecutionError(
            "MCP stdio is disabled: no executable is in MCP_STDIO_ALLOWED_COMMANDS."
        )
    normalized_allowlist = {str(item).strip() for item in allowlist if str(item).strip()}
    resolved = shutil.which(command)
    if not resolved:
        raise ToolExecutionError("MCP stdio command is not installed.")
    # Compare both the exact command and its basename so an operator can pin a
    # full path or a package-managed launcher without opening a shell.
    basename = os.path.basename(command)
    if command not in normalized_allowlist and basename not in normalized_allowlist:
        raise ToolExecutionError("MCP stdio command is not in MCP_STDIO_ALLOWED_COMMANDS.")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
    }
    declared = transport.get("env", {})
    if not isinstance(declared, dict) or len(declared) > 32:
        raise ToolExecutionError("MCP stdio env must be an object with at most 32 entries.")
    for key, reference in declared.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ToolExecutionError("MCP stdio env name is invalid.")
        if not isinstance(reference, str) or not reference.startswith("env:"):
            raise ToolExecutionError(
                "MCP stdio env values must be env: references, never literal secrets."
            )
        value = os.getenv(reference[4:])
        if not value:
            raise ToolExecutionError(f"MCP stdio env {key} is not configured in the runtime.")
        env[key] = value
    return {"command": resolved, "args": arguments, "env": env}


def _open_mcp_client(config, headers, timeout_seconds):
    if config["kind"] == "stdio":
        return McpStdioClient(
            config["command"],
            config["args"],
            config["env"],
            timeout_seconds,
        )
    return McpClient(config["endpoint"], headers, timeout_seconds)


def execute_mcp_operation(arguments, context, tool):
    if not isinstance(arguments, dict):
        raise ToolExecutionError("MCP tool arguments must be a JSON object.")
    config = _mcp_transport_config(context)
    remote_name = config["tool_map"].get(tool.name, tool.name)
    if not isinstance(remote_name, str) or len(remote_name) > 100:
        raise ToolExecutionError("MCP tool mapping produced an invalid tool name.")
    headers = _resolve_auth_headers(context) if config["kind"] == "http" else {}
    timeout_seconds = min(
        max(int(getattr(tool, "timeout_seconds", config["timeout_seconds"]) or 1), 1),
        300,
    )
    with _open_mcp_client(config, headers, timeout_seconds) as client:
        client.initialize()
        return client.call_tool(remote_name, arguments)


def discover_mcp_tools(context):
    """Return the tool descriptors that the configured MCP server exposes."""

    config = _mcp_transport_config(context)
    headers = _resolve_auth_headers(context) if config["kind"] == "http" else {}
    with _open_mcp_client(config, headers, config["timeout_seconds"]) as client:
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
