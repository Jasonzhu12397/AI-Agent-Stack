# ruff: noqa: I001
"""Plugin Studio discovery, schema generation and credential vault.

Users supply a real service address, an optional OpenAPI document and an
authentication mode. This module fetches the declared discovery surface, maps
it onto the platform's closed-object capability contract, generates the
manifest, performs a real authenticated connection test and seals the
credential before anything is persisted.

Nothing here trusts remote input: documents are size bounded, ``$ref`` pointers
are resolved only inside the fetched document, redirects are refused, and every
URL is required to be public HTTPS with a DNS hostname. Generated schemas are
validated by :mod:`core.agent_plugin_manifest` before a definition is stored.
"""

from __future__ import annotations

import json
import re
import socket
from datetime import datetime, UTC
from typing import Any
from urllib.parse import urlsplit

import httpx

from core import plugin_credentials
from core.field_crypto import open_sealed, seal
from core.tool_router import ToolExecutionError

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_TOOLS = 32
MAX_SCHEMA_DEPTH = 8
MAX_PROPERTIES = 64
DEFAULT_TIMEOUT_SECONDS = 15
VERIFIED_PUBLIC_HOSTS: set[str] = set()
CREDENTIAL_SCOPE = "agent-plugin-credential"
CREDENTIAL_PREFIX = "plugin://"

_SUPPORTED_METHODS = {"get", "post"}
_SKIPPED_METHODS = {"put", "patch", "delete", "head", "options", "trace"}
_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}")
_CREDENTIAL_KINDS = {"none", "bearer", "api_key", "oauth2"}
_PLUGIN_TYPES = {"openapi", "mcp", "http_api"}


class PluginStudioError(Exception):
    """A discovery or publish failure with a stable machine code."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _host_error(host: str) -> str | None:
    normalized = (host or "").rstrip(".").lower()
    if not normalized:
        return "A hostname is required."
    if normalized in {"localhost", "metadata.google.internal"} or normalized.endswith(
        (".localhost", ".local", ".internal", ".test", ".example")
    ):
        return "Internal and reserved hostnames are not permitted."
    try:
        import ipaddress

        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return "Use a public DNS hostname, not an IP address."
    labels = normalized.split(".")
    if len(labels) < 2 or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in labels
    ):
        return "The hostname is not a valid public DNS name."
    return None


def validate_public_url(value: Any, field: str = "url") -> str:
    """Validate a self-service plugin URL and prove its DNS is public.

    The resolved address set is checked here and again by the connector at call
    time, which bounds DNS-rebinding between registration and invocation.
    """

    if not isinstance(value, str) or not value or len(value) > 900:
        raise PluginStudioError("PLUGIN_URL_INVALID", f"{field} must be a valid HTTPS URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PluginStudioError("PLUGIN_URL_INVALID", f"{field} is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise PluginStudioError(
            "PLUGIN_URL_INVALID",
            f"{field} must use public HTTPS on the default port, without URL credentials.",
        )
    error = _host_error(parsed.hostname)
    if error:
        raise PluginStudioError("PLUGIN_HOST_REJECTED", error)
    host = parsed.hostname.lower().rstrip(".")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PluginStudioError(
            "PLUGIN_HOST_UNRESOLVED", f"{field} host could not be resolved."
        ) from exc
    resolved = {item[4][0] for item in addresses}
    if not resolved:
        raise PluginStudioError("PLUGIN_HOST_UNRESOLVED", f"{field} host has no addresses.")
    import ipaddress

    for address in resolved:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PluginStudioError(
                "PLUGIN_HOST_REJECTED", f"{field} resolved an invalid address."
            ) from exc
        if not parsed_address.is_global:
            raise PluginStudioError(
                "PLUGIN_HOST_REJECTED",
                f"{field} resolves to a non-public address; SSRF protection blocked it.",
            )
    VERIFIED_PUBLIC_HOSTS.add(host)
    return value.rstrip("/")


def is_verified_public_host(host: str) -> bool:
    return (host or "").lower().rstrip(".") in VERIFIED_PUBLIC_HOSTS


def _auth_from_request(payload: dict[str, Any]) -> dict[str, Any]:
    auth_type = payload.get("auth_type", "none")
    if auth_type not in _CREDENTIAL_KINDS:
        raise PluginStudioError("PLUGIN_AUTH_UNSUPPORTED", "Unsupported authentication mode.")
    auth = payload.get("auth_config") or {}
    credential = payload.get("credential") or ""
    if not isinstance(auth, dict):
        raise PluginStudioError("PLUGIN_AUTH_INVALID", "auth_config must be an object.")
    if auth_type == "none":
        return {"type": "none"}
    if not isinstance(credential, str) or not credential.strip():
        raise PluginStudioError(
            "PLUGIN_CREDENTIAL_REQUIRED", "A credential is required for this authentication mode."
        )
    credential = credential.strip()
    if auth_type == "bearer":
        return {"type": "bearer", "token": credential}
    if auth_type == "api_key":
        header = auth.get("header_name") or "X-API-Key"
        if not isinstance(header, str) or not _HEADER_NAME.fullmatch(header):
            raise PluginStudioError("PLUGIN_AUTH_INVALID", "The API key header name is invalid.")
        prefix = auth.get("header_prefix") or ""
        if not isinstance(prefix, str) or len(prefix) > 64:
            raise PluginStudioError("PLUGIN_AUTH_INVALID", "The API key prefix is invalid.")
        value = f"{prefix}{credential}" if prefix else credential
        return {"type": "header", "name": header, "value": value}
    # OAuth 2.0 client credentials. The token is obtained at call time by the
    # credential resolver; only the sealed client secret is stored.
    token_url = validate_public_url(auth.get("token_url"), "auth_config.token_url")
    client_id = auth.get("client_id")
    scope = auth.get("scope") or ""
    if not isinstance(client_id, str) or not client_id or not isinstance(scope, str):
        raise PluginStudioError("PLUGIN_AUTH_INVALID", "OAuth 2.0 client_id and scope are invalid.")
    return {
        "type": "oauth2",
        "token_url": token_url,
        "client_id": client_id,
        "client_secret": credential,
        "scope": scope,
    }


def credential_headers(contract: dict[str, Any], timeout: float = 15.0) -> dict[str, str]:
    """Turn a credential contract into request headers, acquiring OAuth tokens."""

    kind = contract.get("type")
    if kind in {"none", None}:
        return {}
    if kind == "bearer":
        return {"Authorization": f"Bearer {contract['token']}"}
    if kind == "header":
        return {contract["name"]: contract["value"]}
    if kind == "oauth2":
        data = {
            "grant_type": "client_credentials",
            "client_id": contract["client_id"],
            "client_secret": contract["client_secret"],
        }
        if contract.get("scope"):
            data["scope"] = contract["scope"]
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    validate_public_url(contract["token_url"], "OAuth token URL"),
                    data=data,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise PluginStudioError(
                "PLUGIN_AUTH_UNREACHABLE", "The OAuth token endpoint is unreachable."
            ) from exc
        if response.status_code in {401, 403}:
            raise PluginStudioError(
                "PLUGIN_AUTH_REJECTED", "The OAuth token endpoint rejected the client credentials."
            )
        if response.status_code >= 400:
            raise PluginStudioError(
                "PLUGIN_AUTH_FAILED",
                f"The OAuth token endpoint returned HTTP {response.status_code}.",
            )
        try:
            token = json.loads(response.content.decode("utf-8")).get("access_token")
        except (ValueError, UnicodeDecodeError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise PluginStudioError(
                "PLUGIN_AUTH_FAILED", "The OAuth token response did not contain an access token."
            )
        return {"Authorization": f"Bearer {token}"}
    raise PluginStudioError("PLUGIN_AUTH_UNSUPPORTED", "Unsupported credential contract.")


def _fetch_document(url: str, headers: dict[str, str]) -> tuple[dict[str, Any], int]:
    validate_public_url(url, "document_url")
    try:
        with httpx.Client(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=8.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(url, headers={**headers, "Accept": "application/json"})
    except httpx.ConnectTimeout as exc:
        raise PluginStudioError(
            "PLUGIN_CONNECTION_FAILED", "The discovery document host could not be reached."
        ) from exc
    except httpx.TimeoutException as exc:
        raise PluginStudioError(
            "PLUGIN_CONNECTION_TIMEOUT", "Fetching the discovery document timed out."
        ) from exc
    except httpx.HTTPError as exc:
        raise PluginStudioError(
            "PLUGIN_CONNECTION_FAILED", "The discovery document could not be fetched."
        ) from exc
    if 300 <= response.status_code < 400:
        raise PluginStudioError(
            "PLUGIN_REDIRECT_REFUSED", "Discovery redirects are refused for SSRF protection."
        )
    if response.status_code in {401, 403}:
        raise PluginStudioError(
            "PLUGIN_AUTH_REJECTED",
            f"The service rejected the credential (HTTP {response.status_code}).",
        )
    if response.status_code >= 400:
        raise PluginStudioError(
            "PLUGIN_DOCUMENT_HTTP_ERROR",
            f"The discovery document returned HTTP {response.status_code}.",
        )
    body = response.content
    if len(body) > MAX_DOCUMENT_BYTES:
        raise PluginStudioError(
            "PLUGIN_DOCUMENT_TOO_LARGE", "The discovery document exceeds 1 MiB."
        )
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PluginStudioError(
            "PLUGIN_DOCUMENT_INVALID", "The discovery document must be UTF-8 JSON."
        ) from exc
    if not isinstance(document, dict):
        raise PluginStudioError(
            "PLUGIN_DOCUMENT_INVALID", "The discovery document must be an object."
        )
    if not isinstance(document.get("paths"), dict) or (
        "openapi" not in document and "swagger" not in document
    ):
        raise PluginStudioError(
            "PLUGIN_DOCUMENT_INVALID", "This is not an OpenAPI or Swagger document."
        )
    return document, response.status_code


def _resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise PluginStudioError(
            "PLUGIN_SCHEMA_UNSUPPORTED", "External $ref values are refused during discovery."
        )
    value: Any = document
    for part in pointer[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise PluginStudioError("PLUGIN_SCHEMA_UNSUPPORTED", "A local $ref cannot be resolved.")
        value = value[part]
    return value


def _merge_all_of(schema: dict[str, Any], document: dict[str, Any], depth: int) -> dict[str, Any]:
    merged = {key: value for key, value in schema.items() if key != "allOf"}
    properties = dict(merged.get("properties") or {})
    required = list(merged.get("required") or [])
    for item in schema.get("allOf") or []:
        resolved = _normalize_schema(item, document, depth + 1)
        properties.update(resolved.get("properties") or {})
        required.extend(resolved.get("required") or [])
    merged["properties"] = properties
    merged["required"] = list(dict.fromkeys(required))
    return merged


def _normalize_schema(schema: Any, document: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Reduce OpenAPI schema constructs to the bounded closed-object contract."""

    if depth > MAX_SCHEMA_DEPTH:
        return {"type": "string", "maxLength": 65536}
    if not isinstance(schema, dict):
        return {"type": "string", "maxLength": 65536}
    if "$ref" in schema:
        target = _resolve_pointer(document, str(schema["$ref"]))
        resolved = _normalize_schema(target, document, depth + 1)
        for key in ("description", "default"):
            if key in schema:
                resolved[key] = schema[key]
        return resolved
    if "allOf" in schema:
        schema = _merge_all_of(schema, document, depth)
    if "oneOf" in schema or "anyOf" in schema:
        choices = schema.get("oneOf") or schema.get("anyOf") or []
        if choices:
            return _normalize_schema(choices[0], document, depth + 1)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        candidates = [item for item in schema_type if item != "null"]
        schema_type = candidates[0] if candidates else "string"
    if schema_type is None:
        schema_type = "object" if isinstance(schema.get("properties"), dict) else "string"
    if schema_type == "object":
        properties: dict[str, Any] = {}
        raw_properties = schema.get("properties") or {}
        if isinstance(raw_properties, dict):
            for name, child in list(raw_properties.items())[:MAX_PROPERTIES]:
                if not isinstance(name, str):
                    continue
                properties[name] = _normalize_schema(child, document, depth + 1)
        required = [
            item
            for item in (schema.get("required") or [])
            if isinstance(item, str) and item in properties
        ]
        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if isinstance(schema.get("description"), str):
            result["description"] = schema["description"][:1000]
        return result
    if schema_type == "array":
        items = _normalize_schema(schema.get("items") or {}, document, depth + 1)
        result = {"type": "array", "items": items, "maxItems": 100}
        if isinstance(schema.get("description"), str):
            result["description"] = schema["description"][:1000]
        return result
    allowed = {"string", "integer", "number", "boolean"}
    if schema_type not in allowed:
        schema_type = "string"
    result = {"type": schema_type}
    for key in ("description", "default", "minimum", "maximum", "minLength", "maxLength", "enum"):
        if key in schema:
            result[key] = schema[key]
    if schema_type == "string":
        result.setdefault("maxLength", 65536)
    if schema_type == "array" and "items" not in result:
        result["items"] = {"type": "string", "maxLength": 65536}
    return result


def _safe_identifier(value: str, fallback: str = "tool") -> str:
    candidate = _IDENTIFIER.sub("_", (value or "").strip().lower()).strip("_")
    if not candidate or not candidate[0].isalnum():
        candidate = f"{fallback}_{candidate}" if candidate else fallback
    return candidate[:80]


def _normalize_path(path: str) -> tuple[str, list[str]]:
    """Return a path whose placeholders match the connector's simple grammar."""

    names: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = _safe_identifier(match.group(1), "param")
        names.append(name)
        return "{" + name + "}"

    return re.sub(r"\{([^{}]+)\}", replace, path), names


_RISK_KEYWORDS = {
    "critical": ("delete", "destroy", "drop", "purge", "terminate", "revoke", "reset", "deploy"),
    "high": ("create", "update", "write", "run", "execute", "admin", "disable", "enable"),
}


def _risk_for(method: str, path: str, operation: dict[str, Any]) -> str:
    haystack = f"{path} {operation.get('operationId', '')} {operation.get('summary', '')}".lower()
    for level in ("critical", "high"):
        if any(keyword in haystack for keyword in _RISK_KEYWORDS[level]):
            return level
    return "low" if method == "get" else "medium"


def _parameter_schemas(
    operation: dict[str, Any], document: dict[str, Any], path_names: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: dict[str, str] = {}
    query_params: dict[str, str] = {}
    skipped_headers: list[str] = []
    for parameter in operation.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        if "$ref" in parameter:
            parameter = _resolve_pointer(document, str(parameter["$ref"]))
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or location not in {"path", "query", "header"}:
            continue
        if location == "header":
            skipped_headers.append(name)
            continue
        argument = _safe_identifier(name, location)
        if argument in properties:
            continue
        schema = _normalize_schema(
            parameter.get("schema") or {"type": "string"}, document
        )
        if location == "path" and schema.get("type") not in {"string", "integer"}:
            schema = {"type": "string", "maxLength": 65536}
        if location == "query" and schema.get("type") == "object":
            schema = {"type": "string", "maxLength": 65536}
        properties[argument] = schema
        if parameter.get("required") is True or location == "path":
            required.append(argument)
        if location == "path":
            path_params[argument] = argument
        else:
            query_params[name] = argument
    for placeholder in path_names:
        if placeholder not in path_params:
            properties[placeholder] = {"type": "string", "maxLength": 65536}
            required.append(placeholder)
            path_params[placeholder] = placeholder
    return properties, {"required": required}, path_params, query_params


def _request_body(
    operation: dict[str, Any], document: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None, False
    if "$ref" in body:
        body = _resolve_pointer(document, str(body["$ref"]))
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, dict):
        return None, False
    for media_type, media in content.items():
        if not isinstance(media, dict):
            continue
        if not (media_type == "application/json" or media_type.endswith("+json")):
            continue
        schema = _normalize_schema(media.get("schema") or {"type": "object"}, document)
        if schema.get("type") != "object":
            schema = {
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
                "additionalProperties": False,
            }
        return schema, body.get("required") is True
    return None, False


def _response_text_schema() -> dict[str, Any]:
    # Upstream responses vary; returning a bounded JSON text keeps the closed
    # output contract deterministic without trusting the remote schema.
    return {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": 65536}},
        "required": ["text"],
        "additionalProperties": False,
    }


def discover_openapi(
    *,
    base_url: str,
    document_url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Fetch an OpenAPI document and generate a manifest from its operations."""

    base_url = validate_public_url(base_url, "base_url")
    document, status = _fetch_document(document_url, headers)
    capabilities: list[dict[str, Any]] = []
    operations: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    names: set[str] = set()

    for raw_path, path_item in (document.get("paths") or {}).items():
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            continue
        if not isinstance(path_item, dict):
            continue
        if "$ref" in path_item:
            continue
        path, path_names = _normalize_path(raw_path)
        for raw_method, operation in path_item.items():
            method = str(raw_method).lower()
            if method not in _SUPPORTED_METHODS:
                if method in _SKIPPED_METHODS:
                    skipped.append(
                        {
                            "method": method.upper(),
                            "path": raw_path,
                            "reason": "Method is not executable by the safe HTTP connector.",
                        }
                    )
                continue
            if not isinstance(operation, dict):
                continue
            if len(capabilities) >= MAX_TOOLS:
                skipped.append(
                    {
                        "method": method.upper(),
                        "path": raw_path,
                        "reason": "The 32 capability limit was reached.",
                    }
                )
                continue
            raw_name = operation.get("operationId") or f"{method}_{path}"
            name = _safe_identifier(raw_name)
            base_name = name
            suffix = 2
            while name in names:
                name = f"{base_name}_{suffix}"
                suffix += 1
            names.add(name)
            query_properties, query_required, path_params, query_params = _parameter_schemas(
                operation, document, path_names
            )
            body_schema, body_required = _request_body(operation, document)
            properties = dict(query_properties)
            required = list(query_required["required"])
            if body_schema is not None:
                properties["body"] = body_schema
                if body_required:
                    required.append("body")
            input_schema = {
                "type": "object",
                "properties": properties,
                "required": sorted(dict.fromkeys(required)),
                "additionalProperties": False,
            }
            read_only = method == "get" or operation.get("x-agent-read-only") is True
            risk = "low" if read_only else _risk_for(method, path, operation)
            entry = {
                "name": name,
                "description": str(operation.get("summary") or operation.get("description") or "")[
                    :1000
                ],
                "input_schema": input_schema,
                "output_schema": _response_text_schema(),
                "risk_level": risk,
                "timeout_seconds": 30,
                "source": "declared",
                "remote_operation": f"{method.upper()} {raw_path}",
            }
            capabilities.append(entry)
            operations[name] = {
                "method": method.upper(),
                "path": path,
                "path_params": path_params,
                "query_params": query_params,
                "body_param": "body" if body_schema is not None else None,
                "read_only": read_only,
            }
            if not read_only:
                operations[name]["requires_approval"] = True

    if not capabilities:
        raise PluginStudioError(
            "PLUGIN_NO_CAPABILITIES",
            "No safe GET or POST operations were found in the OpenAPI document.",
        )

    policy = {
        "default": "read_only",
        "approval_required": [
            entry["name"] for entry in capabilities if entry["risk_level"] in {"high", "critical"}
        ],
        "blocked_methods": sorted(method.upper() for method in _SKIPPED_METHODS),
        "max_risk": "critical"
        if any(entry["risk_level"] == "critical" for entry in capabilities)
        else "high",
    }
    manifest = {
        "version": 1,
        "capabilities": capabilities,
        "http": {"base_url": base_url, "operations": operations},
        "permission_policy": policy,
        "studio": {
            "plugin_type": "openapi",
            "document_url": document_url,
            "document_status": status,
            "auth_mode": "configured" if headers else "none",
            "discovered_at": _utc_now(),
        },
    }
    return {
        "protocol": "http_json",
        "endpoint_url": base_url,
        "manifest": manifest,
        "capabilities": capabilities,
        "skipped": skipped,
        "connection": {
            "status": "success",
            "http_status": status,
            "authenticated": bool(headers),
            "message": "OpenAPI document fetched and authenticated.",
        },
    }


def _mcp_headers(contract: dict[str, Any]) -> dict[str, str]:
    return credential_headers(contract) if contract.get("type") != "none" else {}


def discover_mcp(*, endpoint_url: str, contract: dict[str, Any]) -> dict[str, Any]:
    from core.mcp_connector import McpClient

    endpoint_url = validate_public_url(endpoint_url, "base_url")
    headers = _mcp_headers(contract)
    try:
        with McpClient(endpoint_url, headers, DEFAULT_TIMEOUT_SECONDS) as client:
            client.initialize()
            tools = client.list_tools()
    except ToolExecutionError as exc:
        rejected = any(code in str(exc) for code in ("401", "403"))
        code = "PLUGIN_AUTH_REJECTED" if rejected else "PLUGIN_DISCOVERY_FAILED"
        raise PluginStudioError(code, str(exc), 502) from exc
    # The remote list above is authoritative; mapping it directly avoids a
    # second handshake and a credential-resolution pass.
    capabilities = []
    from core.agent_mcp_adapter import _closed_object_schema, _normalize_name

    for tool in tools:
        schema = _closed_object_schema(tool.get("inputSchema") or tool.get("input_schema"))
        if schema is None:
            continue
        normalized = _normalize_name(tool.get("name", ""))
        if normalized is None:
            continue
        local_name, remote_name = normalized
        capabilities.append(
            {
                "name": local_name,
                "description": str(tool.get("description", ""))[:1000],
                "input_schema": schema,
                "output_schema": _response_text_schema(),
                "risk_level": "low",
                "timeout_seconds": 30,
                "source": "discovered",
                "remote_name": remote_name,
            }
        )
        if len(capabilities) >= MAX_TOOLS:
            break
    if not capabilities:
        raise PluginStudioError("PLUGIN_NO_CAPABILITIES", "The MCP server exposed no usable tools.")
    discovered_names = [entry["name"] for entry in capabilities]
    manifest = {
        "version": 1,
        "capabilities": capabilities,
        "mcp": {
            "timeout_seconds": 30,
            "discovery": {
                "endpoint": __import__("hashlib").sha256(endpoint_url.encode()).hexdigest(),
                "discovered_at": _utc_now(),
                "tool_count": len(discovered_names),
                "tools": discovered_names,
            },
        },
        "permission_policy": {
            "default": "read_only",
            "approval_required": [],
            "blocked_methods": [],
            "max_risk": "low",
        },
        "studio": {
            "plugin_type": "mcp",
            "auth_mode": "configured" if headers else "none",
            "discovered_at": _utc_now(),
        },
    }
    return {
        "protocol": "mcp",
        "endpoint_url": endpoint_url,
        "manifest": manifest,
        "capabilities": capabilities,
        "skipped": [],
        "connection": {
            "status": "success",
            "http_status": 200,
            "authenticated": bool(headers),
            "message": f"MCP initialized and listed {len(capabilities)} tools.",
        },
    }


def _probe_document_urls(base_url: str) -> list[str]:
    root = base_url.rstrip("/")
    candidates = [
        root + "/openapi.json",
        root + "/swagger.json",
        root + "/openapi.yaml",
        root + "/docs/openapi.json",
    ]
    return candidates


def discover_plugin(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the whole Studio discovery pipeline for one submitted configuration."""

    plugin_type = payload.get("plugin_type", "openapi")
    if plugin_type not in _PLUGIN_TYPES:
        raise PluginStudioError("PLUGIN_TYPE_UNSUPPORTED", "Unsupported plugin type.")
    base_url = validate_public_url(payload.get("base_url"), "base_url")
    contract = _auth_from_request(payload)
    if plugin_type == "mcp":
        result = discover_mcp(endpoint_url=base_url, contract=contract)
    else:
        document_url = payload.get("document_url")
        if document_url:
            document_url = validate_public_url(document_url, "document_url")
            result = discover_openapi(
                base_url=base_url,
                document_url=document_url,
                headers=credential_headers(contract),
            )
        else:
            errors: list[str] = []
            result = None
            for candidate in _probe_document_urls(base_url):
                try:
                    candidate_url = validate_public_url(candidate, "document_url")
                    result = discover_openapi(
                        base_url=base_url,
                        document_url=candidate_url,
                        headers=credential_headers(contract),
                    )
                    document_url = candidate_url
                    break
                except PluginStudioError as exc:
                    if exc.code in {"PLUGIN_AUTH_REJECTED", "PLUGIN_AUTH_FAILED"}:
                        raise
                    errors.append(str(exc))
            if result is None:
                raise PluginStudioError(
                    "PLUGIN_DOCUMENT_REQUIRED",
                    "No OpenAPI document was supplied and common discovery paths were not found. "
                    + ("; ".join(errors[-2:]) if errors else ""),
                )
        result["manifest"]["studio"]["document_url"] = document_url
        result["manifest"]["studio"]["plugin_type"] = plugin_type
    result["auth_mode"] = contract.get("type", "none")
    result["credential_configured"] = contract.get("type") != "none"
    # Never echo the credential contract; only its kind leaves this function.
    result["credential_kind"] = contract.get("type", "none")
    result["generated"] = {
        "manifest": True,
        "input_schema": True,
        "output_schema": True,
        "auth_schema": True,
        "tool_definitions": len(result["capabilities"]),
        "dynamic_form": True,
        "permission_policy": True,
    }
    from core.agent_plugin_manifest import validate_manifest

    manifest_error = validate_manifest(
        result["manifest"], result["protocol"], endpoint_url=result["endpoint_url"]
    )
    if manifest_error:
        raise PluginStudioError("PLUGIN_SCHEMA_INVALID", manifest_error)
    result["contract"] = contract
    return result


def seal_credential(tenant, user, name: str, contract: dict[str, Any]):
    """Persist a sealed credential contract and return its resolver reference."""

    from core.models import AgentPluginCredential

    if contract.get("type") in {None, "none"}:
        return ""
    sealed = seal(json.dumps(contract, separators=(",", ":")), CREDENTIAL_SCOPE)
    credential = AgentPluginCredential.objects.create(
        tenant=tenant,
        created_by=user,
        name=(name or "plugin credential")[:120],
        sealed_value=sealed,
    )
    return f"{CREDENTIAL_PREFIX}{credential.id}"


def _resolve_oauth2(contract: dict[str, Any]) -> dict[str, str]:
    authorization = credential_headers(contract).get("Authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) <= len("Bearer "):
        raise plugin_credentials.CredentialAccessDeniedError(
            "OAuth token acquisition did not return a bearer token."
        )
    return {"type": "bearer", "token": authorization[len("Bearer ") :]}


def _plugin_credential_provider(reference: str) -> dict[str, str]:
    from core.models import AgentPluginCredential

    if not reference.startswith(CREDENTIAL_PREFIX):
        raise plugin_credentials.CredentialReferenceInvalidError(
            "Plugin credential reference is invalid."
        )
    credential_id = reference[len(CREDENTIAL_PREFIX) :]
    credential = AgentPluginCredential.objects.filter(pk=credential_id).first()
    if credential is None:
        raise plugin_credentials.CredentialNotFoundError("Plugin credential was not found.")
    plaintext = open_sealed(credential.sealed_value, CREDENTIAL_SCOPE)
    if not plaintext:
        raise plugin_credentials.CredentialNotFoundError("Plugin credential could not be read.")
    try:
        contract = json.loads(plaintext)
    except ValueError as exc:
        raise plugin_credentials.CredentialInvalidShapeError(
            "Plugin credential is not a valid JSON contract."
        ) from exc
    if not isinstance(contract, dict):
        raise plugin_credentials.CredentialInvalidShapeError(
            "Plugin credential is not a valid credential contract."
        )
    kind = contract.get("type")
    if kind == "oauth2":
        return _resolve_oauth2(contract)
    if kind not in {"bearer", "header"}:
        raise plugin_credentials.CredentialInvalidShapeError(
            "Plugin credential type is unsupported."
        )
    return contract


def register_credential_provider() -> None:
    plugin_credentials.register_provider("plugin", _plugin_credential_provider)
