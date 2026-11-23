# ruff: noqa: I001
"""Cross-protocol consistency parameterized tests.

The same capability name is installed once per protocol (``http_json``,
``mcp``, ``a2a``, ``openai_compatible``) and walked through one shared set of
assertions. Protocol differences are expressed through the
:class:`ProtocolSpec` parameter table; assertions never branch on the
protocol name. All remotes are stubbed: no real network call is made.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from core import agent_capability_cache
from core.agent_a2a_adapter import (
    CAPABILITY_TIMEOUT as A2A_TIMEOUT,
    CAPABILITY_UPSTREAM_CLIENT_ERROR as A2A_CLIENT_ERROR,
    CAPABILITY_UPSTREAM_INVALID_RESPONSE as A2A_INVALID_RESPONSE,
    CAPABILITY_UPSTREAM_SERVER_ERROR as A2A_SERVER_ERROR,
)
from core.agent_capability_router import (
    CAPABILITY_DISABLED,
    CAPABILITY_INVOCATION_FAILED,
    CAPABILITY_NOT_DECLARED,
    CAPABILITY_NOT_INSTALLED,
    CapabilityResolutionError,
    invoke_capability,
    resolve_capability,
)
from core.agent_openai_compatible_adapter import (
    CAPABILITY_TIMEOUT as OAI_TIMEOUT,
    CAPABILITY_UPSTREAM_CLIENT_ERROR as OAI_CLIENT_ERROR,
    CAPABILITY_UPSTREAM_INVALID_RESPONSE as OAI_INVALID_RESPONSE,
    CAPABILITY_UPSTREAM_SERVER_ERROR as OAI_SERVER_ERROR,
)
from core.models import (
    AgentCapabilityInvocation,
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.tool_router import ToolApprovalRequired

CAPABILITY_NAME = "complete"
FAULT_KEYS = ("client_error", "server_error", "timeout", "invalid_response")
_REAL_CLIENT = httpx.Client
_Handler = Callable[[httpx.Request], httpx.Response]

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "data": {"type": "object"},
    },
    "required": [],
    "additionalProperties": False,
}

TEXT_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

PROMPT_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Manifest factories (one per protocol, registered in the spec table below)
# ---------------------------------------------------------------------------


def _http_json_manifest(risk_level: str) -> dict[str, Any]:
    return {
        "http": {
            "base_url": "https://http.example.com",
            "operations": {
                CAPABILITY_NAME: {
                    "method": "GET",
                    "path": "/v1/complete",
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": CAPABILITY_NAME,
                "description": "Complete a prompt over HTTP JSON",
                "input_schema": TEXT_INPUT,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": risk_level,
                "timeout_seconds": 5,
            }
        ],
    }


def _mcp_manifest(risk_level: str) -> dict[str, Any]:
    return {
        "mcp": {"timeout_seconds": 10},
        "capabilities": [
            {
                "name": CAPABILITY_NAME,
                "description": "Complete a prompt over MCP",
                "input_schema": TEXT_INPUT,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": risk_level,
                "timeout_seconds": 10,
                "remote_name": CAPABILITY_NAME,
            }
        ],
    }


def _a2a_manifest(risk_level: str) -> dict[str, Any]:
    return {
        "a2a": {"agent_card_path": "/.well-known/agent-card.json", "timeout_seconds": 10},
        "capabilities": [
            {
                "name": CAPABILITY_NAME,
                "description": "Complete a prompt over A2A",
                "input_schema": PROMPT_INPUT,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": risk_level,
                "timeout_seconds": 10,
                "remote_name": CAPABILITY_NAME,
            }
        ],
    }


def _openai_manifest(risk_level: str) -> dict[str, Any]:
    return {
        "openai_compatible": {
            "base_url": "https://oai.example.com",
            "model": "example-model",
            "path": "/v1/chat/completions",
            "timeout_seconds": 10,
        },
        "capabilities": [
            {
                "name": CAPABILITY_NAME,
                "description": "Complete a prompt over OpenAI-compatible API",
                "input_schema": PROMPT_INPUT,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": risk_level,
                "timeout_seconds": 10,
            }
        ],
    }


@dataclass(frozen=True)
class ProtocolSpec:
    """One parameterized protocol installation under test."""

    protocol: str
    host: str
    endpoint: str
    plugin_key: str
    arguments: dict[str, Any]
    manifest_factory: Callable[[str], dict[str, Any]]
    # fault key -> normalized raised error code expected from the adapter.
    expected_raised: dict[str, str]


# The shared expectation table. ``http_json`` and ``mcp`` route every transport
# fault through their shared connector's generic ``ToolExecutionError``; the
# router normalizes those to ``CAPABILITY_INVOCATION_FAILED``. The A2A and
# OpenAI adapters classify status/timeout/payload faults into distinct codes.
SPECS: list[ProtocolSpec] = [
    ProtocolSpec(
        protocol="http_json",
        host="http.example.com",
        endpoint="https://http.example.com/v1/invoke",
        plugin_key="http-consistency-agent",
        arguments={"text": "hello"},
        manifest_factory=_http_json_manifest,
        expected_raised={
            "client_error": CAPABILITY_INVOCATION_FAILED,
            "server_error": CAPABILITY_INVOCATION_FAILED,
            "timeout": CAPABILITY_INVOCATION_FAILED,
            "invalid_response": CAPABILITY_INVOCATION_FAILED,
        },
    ),
    ProtocolSpec(
        protocol="mcp",
        host="mcp.example.com",
        endpoint="https://mcp.example.com/rpc",
        plugin_key="mcp-consistency-agent",
        arguments={"text": "hello"},
        manifest_factory=_mcp_manifest,
        expected_raised={
            "client_error": CAPABILITY_INVOCATION_FAILED,
            "server_error": CAPABILITY_INVOCATION_FAILED,
            "timeout": CAPABILITY_INVOCATION_FAILED,
            "invalid_response": CAPABILITY_INVOCATION_FAILED,
        },
    ),
    ProtocolSpec(
        protocol="a2a",
        host="a2a.example.com",
        endpoint="https://a2a.example.com/agent",
        plugin_key="a2a-consistency-agent",
        arguments={"prompt": "hello"},
        manifest_factory=_a2a_manifest,
        expected_raised={
            "client_error": A2A_CLIENT_ERROR,
            "server_error": A2A_SERVER_ERROR,
            "timeout": A2A_TIMEOUT,
            "invalid_response": A2A_INVALID_RESPONSE,
        },
    ),
    ProtocolSpec(
        protocol="openai_compatible",
        host="oai.example.com",
        endpoint="https://oai.example.com/v1/chat/completions",
        plugin_key="oai-consistency-agent",
        arguments={"prompt": "hello"},
        manifest_factory=_openai_manifest,
        expected_raised={
            "client_error": OAI_CLIENT_ERROR,
            "server_error": OAI_SERVER_ERROR,
            "timeout": OAI_TIMEOUT,
            "invalid_response": OAI_INVALID_RESPONSE,
        },
    ),
]

SPEC_PARAMS = [pytest.param(spec, id=spec.protocol) for spec in SPECS]


# ---------------------------------------------------------------------------
# Database fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture
def consistency_workspace(db):
    agent = get_user_model().objects.create_user(
        username="consistency-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Consistency tenant", slug="consistency-tenant")
    TenantMembership.objects.create(tenant=tenant, user=agent, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(
        tenant=tenant, name="Consistency project", slug="consistency-project"
    )
    ProjectMembership.objects.create(project=project, user=agent, role=ProjectMembership.Role.ADMIN)
    agent_capability_cache.clear()
    return agent, tenant, project


def _make_plugin(
    tenant: Any,
    user: Any,
    spec: ProtocolSpec,
    *,
    risk_level: str = "low",
) -> AgentPluginDefinition:
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=spec.plugin_key,
        name=spec.plugin_key.title(),
        version="1.0.0",
        protocol=spec.protocol,
        endpoint_url=spec.endpoint,
        auth_required=False,
        capabilities=[CAPABILITY_NAME],
        manifest=spec.manifest_factory(risk_level),
    )


def _install(
    tenant: Any,
    project: Any,
    plugin: AgentPluginDefinition,
    user: Any,
    *,
    status: str = "installed",
) -> AgentPluginInstallation:
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=status,
        granted_capabilities=[CAPABILITY_NAME],
        credential_ref="",
        installed_by=user,
    )


# ---------------------------------------------------------------------------
# Stub transport layers (data-driven: one patcher per protocol in the table)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, content_type: str) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body

    def iter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeHttpJsonClient:
    def __init__(
        self,
        response: _FakeResponse | None,
        captured: list[dict[str, Any]],
        side_effect: Exception | None,
    ) -> None:
        self._response = response
        self._captured = captured
        self._side_effect = side_effect

    def __enter__(self) -> "_FakeHttpJsonClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStream:
        self._captured.append({"method": method, "url": url})
        if self._side_effect is not None:
            raise self._side_effect
        return _FakeStream(self._response)


def _http_json_fault_response(fault: str) -> _FakeResponse:
    if fault == "client_error":
        return _FakeResponse(400, b'{"error":"bad request"}', "application/json")
    if fault == "server_error":
        return _FakeResponse(500, b'{"error":"boom"}', "application/json")
    return _FakeResponse(200, b"<html>not json</html>", "text/html")


def _patch_http_json(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProtocolSpec,
    calls: list[dict[str, Any]],
    fault: str | None,
) -> None:
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    side_effect = httpx.TimeoutException("timed out") if fault == "timeout" else None
    if fault is None:
        response = _FakeResponse(
            200, json.dumps({"text": "done"}).encode("utf-8"), "application/json"
        )
    else:
        response = _http_json_fault_response(fault)
    monkeypatch.setattr(
        "core.http_connector.httpx.Client",
        lambda **kwargs: _FakeHttpJsonClient(response, calls, side_effect),
    )


def _rpc(request_id: Any, result: Any) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8"),
    )


def _mcp_handler(calls: list[dict[str, Any]], fault: str | None) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        if fault == "timeout":
            raise httpx.TimeoutException("timed out")
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-1", "content-type": "application/json"},
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                    }
                ).encode("utf-8"),
            )
        if method == "tools/list":
            return _rpc(
                body.get("id"),
                {
                    "tools": [
                        {
                            "name": CAPABILITY_NAME,
                            "description": "Complete a prompt",
                            "inputSchema": TEXT_INPUT,
                        }
                    ]
                },
            )
        if method == "tools/call":
            if fault == "client_error":
                return httpx.Response(400, content=b"{}")
            if fault == "server_error":
                return httpx.Response(500, content=b"{}")
            if fault == "invalid_response":
                return httpx.Response(200, content=b"not-json")
            return _rpc(
                body.get("id"),
                {
                    "content": [{"type": "text", "text": "done"}],
                    "isError": False,
                },
            )
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    return handler


def _a2a_handler(calls: list[dict[str, Any]], fault: str | None) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        if fault == "timeout":
            raise httpx.TimeoutException("timed out")
        if fault == "client_error":
            return httpx.Response(400, content=b'{"error":"bad request"}')
        if fault == "server_error":
            return httpx.Response(500, content=b'{"error":"boom"}')
        if fault == "invalid_response":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>not json</html>"
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"artifacts": [{"text": "done"}]},
                }
            ).encode("utf-8"),
        )

    return handler


def _openai_handler(calls: list[dict[str, Any]], fault: str | None) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        if fault == "timeout":
            raise httpx.TimeoutException("timed out")
        if fault == "client_error":
            return httpx.Response(400, content=b'{"error":"bad request"}')
        if fault == "server_error":
            return httpx.Response(500, content=b'{"error":"boom"}')
        if fault == "invalid_response":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>not json</html>"
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {"choices": [{"message": {"content": "done"}}]}
            ).encode("utf-8"),
        )

    return handler


def _patch_mcp(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProtocolSpec,
    calls: list[dict[str, Any]],
    fault: str | None,
) -> None:
    def factory(**kwargs: Any) -> httpx.Client:
        return _REAL_CLIENT(
            transport=httpx.MockTransport(_mcp_handler(calls, fault)),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.mcp_connector.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


def _patch_a2a(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProtocolSpec,
    calls: list[dict[str, Any]],
    fault: str | None,
) -> None:
    def factory(**kwargs: Any) -> httpx.Client:
        return _REAL_CLIENT(
            transport=httpx.MockTransport(_a2a_handler(calls, fault)),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.agent_a2a_adapter.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.36", 443))],
    )


def _patch_openai(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProtocolSpec,
    calls: list[dict[str, Any]],
    fault: str | None,
) -> None:
    def factory(**kwargs: Any) -> httpx.Client:
        return _REAL_CLIENT(
            transport=httpx.MockTransport(_openai_handler(calls, fault)),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.agent_openai_compatible_adapter.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.35", 443))],
    )


PATCHERS: dict[str, Callable[..., None]] = {
    "http_json": _patch_http_json,
    "mcp": _patch_mcp,
    "a2a": _patch_a2a,
    "openai_compatible": _patch_openai,
}


def _stub_remote(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProtocolSpec,
    calls: list[dict[str, Any]],
    fault: str | None,
) -> None:
    PATCHERS[spec.protocol](monkeypatch, spec, calls, fault)


# ---------------------------------------------------------------------------
# Shared record-shape assertion reused by every protocol instance
# ---------------------------------------------------------------------------


def _assert_record_shape(
    installation: AgentPluginInstallation,
    spec: ProtocolSpec,
    status: str,
    error_code: str,
) -> AgentCapabilityInvocation:
    records = AgentCapabilityInvocation.objects.filter(installation=installation)
    assert records.count() == 1
    record = records.get()
    assert record.capability == CAPABILITY_NAME
    assert record.protocol == spec.protocol
    assert record.status == status
    assert record.error_code == error_code
    assert record.latency_ms >= 0
    return record


# ---------------------------------------------------------------------------
# Invariant 1: resolution consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_resolve_matches_declared_capability(consistency_workspace, spec):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    installation = _install(tenant, project, plugin, user)

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)

    assert resolved.capability_name == CAPABILITY_NAME
    assert resolved.installation.id == installation.id
    assert resolved.installation is not None
    assert resolved.plugin.protocol == spec.protocol


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_resolve_undeclared_capability_rejected(consistency_workspace, spec):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    _install(tenant, project, plugin, user)

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "missing_task")
    assert exc.value.code == CAPABILITY_NOT_DECLARED


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_resolve_uninstalled_capability_rejected(consistency_workspace, spec):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    _install(tenant, project, plugin, user, status="uninstalled")

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert exc.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_resolve_disabled_capability_rejected(consistency_workspace, spec):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    _install(tenant, project, plugin, user, status="disabled")

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert exc.value.code == CAPABILITY_DISABLED


# ---------------------------------------------------------------------------
# Invariant 2: approval gate consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_high_risk_rejected_without_approval_and_zero_remote_call(
    consistency_workspace, monkeypatch, spec
):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec, risk_level="high")
    installation = _install(tenant, project, plugin, user)
    calls: list[dict[str, Any]] = []
    _stub_remote(monkeypatch, spec, calls, fault=None)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[spec.host]):
        with pytest.raises(ToolApprovalRequired):
            invoke_capability(
                str(tenant.id),
                str(project.id),
                CAPABILITY_NAME,
                dict(spec.arguments),
                approved=False,
            )

    assert calls == []
    record = _assert_record_shape(
        installation, spec, AgentCapabilityInvocation.Status.REJECTED, "approval_required"
    )
    assert record.latency_ms == 0


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_high_risk_succeeds_after_approval(consistency_workspace, monkeypatch, spec):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec, risk_level="high")
    installation = _install(tenant, project, plugin, user)
    calls: list[dict[str, Any]] = []
    _stub_remote(monkeypatch, spec, calls, fault=None)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[spec.host]):
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            CAPABILITY_NAME,
            dict(spec.arguments),
            approved=True,
        )

    assert result["text"] == "done"
    _assert_record_shape(
        installation, spec, AgentCapabilityInvocation.Status.SUCCESS, ""
    )


# ---------------------------------------------------------------------------
# Invariant 3: invocation record consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_success_invocation_record_shape_consistent(
    consistency_workspace, monkeypatch, spec
):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    installation = _install(tenant, project, plugin, user)
    calls: list[dict[str, Any]] = []
    _stub_remote(monkeypatch, spec, calls, fault=None)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[spec.host]):
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            CAPABILITY_NAME,
            dict(spec.arguments),
        )

    assert result["text"] == "done"
    _assert_record_shape(
        installation, spec, AgentCapabilityInvocation.Status.SUCCESS, ""
    )


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
def test_failure_invocation_record_shape_consistent(
    consistency_workspace, monkeypatch, spec
):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    installation = _install(tenant, project, plugin, user)
    calls: list[dict[str, Any]] = []
    _stub_remote(monkeypatch, spec, calls, fault="client_error")

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[spec.host]):
        with pytest.raises(CapabilityResolutionError):
            invoke_capability(
                str(tenant.id),
                str(project.id),
                CAPABILITY_NAME,
                dict(spec.arguments),
            )

    _assert_record_shape(
        installation, spec, AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED
    )


# ---------------------------------------------------------------------------
# Invariant 4: upstream fault code normalization consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("spec", SPEC_PARAMS)
@pytest.mark.parametrize("fault", FAULT_KEYS)
def test_upstream_fault_normalization(consistency_workspace, monkeypatch, spec, fault):
    user, tenant, project = consistency_workspace
    plugin = _make_plugin(tenant, user, spec)
    installation = _install(tenant, project, plugin, user)
    calls: list[dict[str, Any]] = []
    _stub_remote(monkeypatch, spec, calls, fault)

    expected_code = spec.expected_raised[fault]
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[spec.host]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                CAPABILITY_NAME,
                dict(spec.arguments),
            )

    assert exc.value.code == expected_code
    # Persisted failure records always normalize to the router-owned failure code.
    _assert_record_shape(
        installation, spec, AgentCapabilityInvocation.Status.FAILED, CAPABILITY_INVOCATION_FAILED
    )
