# ruff: noqa: I001
import ipaddress
import json
import socket
from urllib.parse import quote, urlencode, urlsplit

import httpx
from django.conf import settings

from core.tool_router import ToolExecutionError


MAX_RESPONSE_BYTES = 1048576
MAX_QUERY_ARGUMENT_CHARS = 2048
DEFAULT_TIMEOUT_SECONDS = 60


def _reject_json_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _validate_base_url(url):
    if not isinstance(url, str) or len(url) > 500:
        raise ToolExecutionError("HTTP base URL is invalid.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolExecutionError("HTTP base URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or parsed.query
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ToolExecutionError("HTTP connectors require an HTTPS origin on port 443.")
    host = parsed.hostname.lower().rstrip(".")
    allowed_hosts = set(getattr(settings, "PLUGIN_HTTP_ALLOWED_HOSTS", []))
    if host not in allowed_hosts:
        raise ToolExecutionError("HTTP connector host is not in the platform allowlist.")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolExecutionError("HTTP connector host could not be resolved.") from exc
    for item in addresses:
        address = item[4][0]
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ToolExecutionError("HTTP connector resolved an invalid address.") from exc
        if not parsed_address.is_global:
            raise ToolExecutionError("HTTP connector host resolves to a non-public address.")
    if not addresses:
        raise ToolExecutionError("HTTP connector host has no resolved addresses.")
    return parsed


def _load_json(payload):
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ToolExecutionError("Connector response is not valid UTF-8 JSON.") from exc


class _CredentialMissing(ToolExecutionError):
    pass


def _resolve_auth_headers(context):
    auth_type = getattr(context, "connection_auth_type", "none")
    if auth_type in (None, "", "none"):
        return {}
    if auth_type != "bearer":
        raise ToolExecutionError("Connector auth type is not supported.")
    credential_ref = getattr(context, "credential_ref", "") or ""
    if not credential_ref:
        raise _CredentialMissing("Connector credential reference is missing.")
    secret = None
    resolver = getattr(context, "resolve_credential", None)
    if callable(resolver):
        secret = resolver(credential_ref)
    if not secret:
        credential = getattr(context, "credential", None)
        if credential is not None and getattr(credential, "reference", "") == credential_ref:
            secret = getattr(credential, "secret", "")
    if not secret:
        raise _CredentialMissing("Connector credential could not be resolved.")
    return {"Authorization": f"Bearer {secret}"}


def _build_query(arguments, query_params):
    pairs = []
    for parameter in query_params:
        if not isinstance(parameter, str) or len(parameter) > 64:
            raise ToolExecutionError("Connector query parameter name is invalid.")
        if parameter not in arguments:
            continue
        value = arguments[parameter]
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ToolExecutionError("Connector query argument must be a scalar.")
        text = str(value)
        if len(text) > MAX_QUERY_ARGUMENT_CHARS:
            raise ToolExecutionError("Connector query argument is too long.")
        pairs.append((parameter, text))
    used = {name for name, _ in pairs}
    return pairs, used


def _build_body(arguments, body_params):
    body = {}
    used = set()
    for parameter in body_params:
        if not isinstance(parameter, str) or len(parameter) > 64:
            raise ToolExecutionError("Connector body parameter name is invalid.")
        if parameter not in arguments:
            continue
        body[parameter] = arguments[parameter]
        used.add(parameter)
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ToolExecutionError("Connector request body is too large.")
    return encoded, used


def _read_body(response):
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ToolExecutionError("Connector response exceeds 1 MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


def _render_path(template, arguments):
    if not isinstance(template, str) or len(template) > 500:
        raise ToolExecutionError("Connector path template is invalid.")
    if not template.startswith("/"):
        raise ToolExecutionError("Connector path template must be absolute.")
    used = set()
    rendered = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == "{":
            end = template.find("}", index)
            if end == -1:
                raise ToolExecutionError("Connector path template is malformed.")
            name = template[index + 1 : end]
            if not name or not name.replace("_", "").replace("-", "").isalnum():
                raise ToolExecutionError("Connector path parameter is invalid.")
            if name not in arguments:
                raise ToolExecutionError("Connector path parameter is missing.")
            value = arguments[name]
            if value is None:
                raise ToolExecutionError("Connector path parameter is missing.")
            text = str(value)
            if len(text) > MAX_QUERY_ARGUMENT_CHARS:
                raise ToolExecutionError("Connector path parameter is too long.")
            rendered.append(quote(text, safe=""))
            used.add(name)
            index = end + 1
            continue
        rendered.append(char)
        index += 1
    return "".join(rendered), used


def _connector_config(context, tool):
    manifest = context.plugin_manifest
    if not isinstance(manifest, dict):
        raise ToolExecutionError("Connector manifest is missing.")
    transport = manifest.get("http")
    if not isinstance(transport, dict):
        raise ToolExecutionError("HTTP transport manifest is missing.")
    method = str(transport.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ToolExecutionError("Connector HTTP method is not supported.")
    body_params = transport.get("body_params", [])
    if not isinstance(body_params, list):
        raise ToolExecutionError("Connector body parameters are invalid.")
    query_params = transport.get("query_params", [])
    if not isinstance(query_params, list):
        raise ToolExecutionError("Connector query parameters are invalid.")
    timeout_seconds = manifest.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return transport, method, body_params, query_params, timeout_seconds


def execute_http_operation(arguments, context, tool):
    if not isinstance(arguments, dict):
        raise ToolExecutionError("Connector arguments must be a JSON object.")
    transport, method, body_params, query_params, timeout_seconds = _connector_config(
        context, tool
    )
    base_url = _validate_base_url(transport.get("base_url"))
    timeout_seconds = min(max(int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), 1), 300)
    timeout_seconds = min(
        max(int(getattr(tool, "timeout_seconds", timeout_seconds) or 1), 1), 300
    )
    path, path_used = _render_path(transport.get("path", "/"), arguments)
    query_pairs, query_used = _build_query(arguments, query_params)
    body_bytes, body_used = _build_body(arguments, body_params)
    used = path_used | query_used | body_used
    unknown = set(arguments) - used
    if unknown:
        raise ToolExecutionError("Connector arguments contain unknown fields.")

    url = f"https://{base_url.hostname}{path}"
    if query_pairs:
        url = f"{url}?{urlencode(query_pairs)}"
    headers = _resolve_auth_headers(context)
    headers.setdefault("Accept", "application/json")
    if method in {"POST", "PUT", "PATCH"}:
        headers.setdefault("Content-Type", "application/json")

    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                method,
                url,
                content=body_bytes if method in {"POST", "PUT", "PATCH"} else None,
                headers=headers,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise ToolExecutionError("Connector redirects are not allowed.")
                if not 200 <= response.status_code < 300:
                    raise ToolExecutionError("Connector endpoint returned a non-success status.")
                payload = _read_body(response)
    except ToolExecutionError:
        raise
    except httpx.HTTPError:
        raise ToolExecutionError("Connector request failed.") from None

    if not payload:
        return {}
    result = _load_json(payload)
    if not isinstance(result, dict):
        raise ToolExecutionError("Connector response must be a JSON object.")
    return result


def register_connector_tool(router, tool):
    """Register a connector tool with the runtime that owns its protocol."""

    if tool.plugin.connector_type == "mcp":
        from core.mcp_connector import register_mcp_tool

        return register_mcp_tool(router, tool)
    return _register_http_tool(router, tool)


def register_http_tool(router, tool):
    """Backwards-compatible entry point used by existing call sites."""

    return register_connector_tool(router, tool)


def _register_http_tool(router, tool):
    if tool.plugin.connector_type not in {"http", "openapi"}:
        return False
    if router.has_handler(
        tool.plugin.plugin_key,
        tool.name,
        plugin_version=tool.plugin.version,
    ):
        return False

    def handler(arguments, context):
        return execute_http_operation(arguments, context, tool)

    router.register(
        tool.plugin.plugin_key,
        tool.name,
        handler,
        plugin_version=tool.plugin.version,
    )
    return True
