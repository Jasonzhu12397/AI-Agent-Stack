# ruff: noqa: I001
import ipaddress
import json
import re
import socket
from urllib.parse import quote, urlsplit

import httpx

from core.tool_router import ToolContext, ToolExecutionError
from django.conf import settings
from django.utils.module_loading import import_string


HTTP_METHODS = {"GET", "POST"}
PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}")


def _reject_json_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _validate_base_url(base_url):
    if not isinstance(base_url, str) or len(base_url) > 500:
        raise ToolExecutionError("HTTP connector base URL is invalid.")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ToolExecutionError("HTTP connector base URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or re.search(r"%2e", parsed.path, re.IGNORECASE)
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
    resolved = {item[4][0] for item in addresses}
    if not resolved:
        raise ToolExecutionError("HTTP connector host has no resolved addresses.")
    for address in resolved:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ToolExecutionError("HTTP connector resolved an invalid address.") from exc
        if not parsed_address.is_global:
            raise ToolExecutionError("HTTP connector host resolves to a non-public address.")
    return base_url.rstrip("/")


def _resolve_auth(context):
    if context.connection_auth_type == "none":
        return {}, None
    if not context.credential_ref:
        raise ToolExecutionError("Connector credential reference is missing.")
    resolver_path = getattr(settings, "PLUGIN_CREDENTIAL_RESOLVER", "")
    if not resolver_path:
        raise ToolExecutionError("No credential resolver is configured for this connector.")
    try:
        resolver = import_string(resolver_path)
        credential = resolver(context.credential_ref)
    except Exception:
        raise ToolExecutionError("Connector credential could not be resolved.") from None
    if not isinstance(credential, dict):
        raise ToolExecutionError("Credential resolver returned an invalid credential.")
    credential_type = credential.get("type")
    if credential_type == "bearer":
        token = credential.get("token")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ToolExecutionError("Credential resolver returned an invalid bearer token.")
        return {"Authorization": "Bearer " + token}, None
    if credential_type == "header":
        name = credential.get("name")
        value = credential.get("value")
        if (
            not isinstance(name, str)
            or not HEADER_NAME.fullmatch(name)
            or not isinstance(value, str)
            or not value
            or "\n" in value
            or "\r" in value
            or name.lower() in {"host", "content-length", "transfer-encoding"}
        ):
            raise ToolExecutionError("Credential resolver returned an invalid header credential.")
        return {name: value}, None
    if credential_type == "query":
        name = credential.get("name")
        value = credential.get("value")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name)
            or not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(char) < 32 for char in value)
        ):
            raise ToolExecutionError("Credential resolver returned an invalid query credential.")
        return {}, (name, value)
    raise ToolExecutionError("Credential resolver returned an unsupported credential type.")


def _resolve_auth_headers(context):
    headers, query_credential = _resolve_auth(context)
    if query_credential is not None:
        raise ToolExecutionError("Query credentials require an explicitly configured operation.")
    return headers


def _operation_config(context, tool):
    transport = context.plugin_manifest.get("http")
    if not isinstance(transport, dict):
        raise ToolExecutionError("Connector HTTP transport manifest is missing.")
    operations = transport.get("operations")
    if not isinstance(operations, dict):
        raise ToolExecutionError("Connector HTTP operation map is invalid.")
    operation = operations.get(tool.name)
    if not isinstance(operation, dict):
        raise ToolExecutionError("Connector HTTP operation is not configured.")
    base_url = transport.get("base_url")
    parsed_base = _validate_base_url(base_url)
    method = operation.get("method")
    path = operation.get("path")
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        raise ToolExecutionError("Connector HTTP method is not allowed.")
    if (
        method.upper() == "POST"
        and not (
            operation.get("read_only") is True
            and tool.idempotency_mode == "read_only"
        )
    ):
        raise ToolExecutionError(
            "POST is allowed only for explicitly read-only connector operations."
        )
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or "?" in path
        or "#" in path
        or re.search(r"%2e", path, re.IGNORECASE)
        or any(ord(char) < 32 for char in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ToolExecutionError("Connector HTTP path is invalid.")
    if len(path) > 1000:
        raise ToolExecutionError("Connector HTTP path is too long.")
    placeholders = set(PATH_PARAMETER.findall(path))
    path_params = operation.get("path_params", {})
    query_params = operation.get("query_params", {})
    body_param = operation.get("body_param")
    configured_headers = operation.get("headers", {})
    auth_query_parameter = operation.get("auth_query_parameter")
    if not isinstance(configured_headers, dict):
        raise ToolExecutionError("Connector operation headers are invalid.")
    static_headers = {}
    static_header_names = set()
    forbidden_headers = {
        "authorization", "proxy-authorization", "cookie", "set-cookie",
        "host", "content-length", "transfer-encoding", "connection",
        "upgrade", "te", "trailer", "expect", "x-goog-api-key", "x-api-key",
    }
    for name, value in configured_headers.items():
        if (
            not isinstance(name, str)
            or not HEADER_NAME.fullmatch(name)
            or name.lower() in forbidden_headers
            or not isinstance(value, str)
            or not value
            or len(value) > 2048
            or "\n" in value
            or "\r" in value
        ):
            raise ToolExecutionError("Connector operation contains an unsafe static header.")
        normalized_name = name.lower()
        if normalized_name in static_header_names:
            raise ToolExecutionError("Connector operation contains duplicate static headers.")
        static_header_names.add(normalized_name)
        static_headers[name] = value
    if auth_query_parameter is not None and (
        not isinstance(auth_query_parameter, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", auth_query_parameter)
    ):
        raise ToolExecutionError("Connector authentication query parameter is invalid.")
    if (
        not isinstance(path_params, dict)
        or not isinstance(query_params, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in path_params.items()
        )
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in query_params.items()
        )
    ):
        raise ToolExecutionError("Connector HTTP parameter mapping is invalid.")
    if set(path_params) != placeholders:
        raise ToolExecutionError("Connector HTTP path parameter mapping is incomplete.")
    if auth_query_parameter is not None and auth_query_parameter in query_params:
        raise ToolExecutionError(
            "Connector authentication query parameter conflicts with an operation mapping."
        )
    if transport.get("auth_required") is True and (
        context.connection_auth_type == "none" or not context.credential_ref
    ):
        raise ToolExecutionError("Connector requires an authenticated connection.")
    mapped_args = set(path_params.values()) | set(query_params.values())
    if body_param is not None:
        if not isinstance(body_param, str) or not body_param:
            raise ToolExecutionError("Connector HTTP body parameter mapping is invalid.")
        mapped_args.add(body_param)
    if len(mapped_args) != len(path_params) + len(query_params) + (1 if body_param else 0):
        raise ToolExecutionError("Connector HTTP argument mapping contains duplicates.")
    return (
        parsed_base,
        method.upper(),
        path,
        path_params,
        query_params,
        body_param,
        static_headers,
        auth_query_parameter,
    )


def execute_http_operation(arguments, context: ToolContext, tool):
    (
        base,
        method,
        path,
        path_params,
        query_params,
        body_param,
        static_headers,
        auth_query_parameter,
    ) = _operation_config(
        context,
        tool,
    )
    mapped_args = set(path_params.values()) | set(query_params.values())
    if body_param:
        mapped_args.add(body_param)
    supplied_args = set(arguments)
    required_args = set(path_params.values())
    if body_param:
        required_args.add(body_param)
    if not supplied_args.issubset(mapped_args) or not required_args.issubset(supplied_args):
        raise ToolExecutionError("Connector HTTP argument mapping does not match its input schema.")

    path_values = {}
    for placeholder, argument_name in path_params.items():
        value = arguments.get(argument_name)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ToolExecutionError("Connector path parameters must be strings or integers.")
        if len(str(value)) > 2048:
            raise ToolExecutionError("Connector path parameter is too long.")
        path_values[placeholder] = quote(str(value), safe="")
    rendered_path = PATH_PARAMETER.sub(
        lambda match: path_values[match.group(1)],
        path,
    )
    if "{" in rendered_path or "}" in rendered_path:
        raise ToolExecutionError("Connector HTTP path contains an unresolved parameter.")

    query = []
    for key, argument_name in query_params.items():
        if argument_name not in arguments:
            continue
        value = arguments[argument_name]
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, (dict, list)) or item is None or isinstance(item, bool):
                raise ToolExecutionError("Connector query parameters must be scalar values.")
            query.append((key, str(item)))

    auth_headers, auth_query = _resolve_auth(context)
    auth_header_names = {name.lower() for name in auth_headers}
    if (
        auth_header_names.intersection({name.lower() for name in static_headers})
        or "accept" in auth_header_names
    ):
        raise ToolExecutionError(
            "Connector authentication headers conflict with platform or operation headers."
        )
    if auth_query is not None:
        if auth_query_parameter != auth_query[0]:
            raise ToolExecutionError(
                "Connector query credential does not match the operation configuration."
            )
        if any(key == auth_query[0] for key, _ in query):
            raise ToolExecutionError(
                "Connector query credential conflicts with an operation argument."
            )
        query.append(auth_query)
    elif auth_query_parameter is not None:
        raise ToolExecutionError("Connector operation requires a query credential.")

    body = arguments.get(body_param) if body_param else None
    if body is not None:
        try:
            encoded_body = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("Connector request body is not valid JSON.") from exc
        if len(encoded_body) > 65536:
            raise ToolExecutionError("Connector request body exceeds 64 KiB.")
    if method == "GET" and body is not None:
        raise ToolExecutionError("GET connector operations cannot send a request body.")

    url = str(base).rstrip("/") + rendered_path
    headers = {"Accept": "application/json"}
    headers.update(auth_headers)
    headers.update(static_headers)
    timeout_seconds = min(max(int(tool.timeout_seconds), 1), 300)
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10))
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                method,
                url,
                params=query,
                json=body,
                headers=headers,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise ToolExecutionError("Connector redirects are not allowed.")
                if not 200 <= response.status_code < 300:
                    raise ToolExecutionError("Connector returned a non-success HTTP status.")
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if not (
                    content_type == "application/json"
                    or content_type.endswith("+json")
                ):
                    raise ToolExecutionError("Connector response content type must be JSON.")
                chunks = []
                total_bytes = 0
                for chunk in response.iter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > 1048576:
                        raise ToolExecutionError("Connector response exceeds 1 MiB.")
                    chunks.append(chunk)
                payload = b"".join(chunks)
    except ToolExecutionError:
        raise
    except httpx.HTTPError:
        raise ToolExecutionError("Connector HTTP request failed.") from None

    try:
        result = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ToolExecutionError("Connector response must be a UTF-8 JSON object.") from exc
    if not isinstance(result, dict):
        raise ToolExecutionError("Connector response must be a JSON object.")
    return result


def register_http_tool(router, tool):
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
