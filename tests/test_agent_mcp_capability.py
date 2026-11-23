# ruff: noqa: I001
import json

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from core import agent_plugin_manifest
from core.agent_capability_router import (
    CAPABILITY_INVOCATION_FAILED,
    CapabilityResolutionError,
    invoke_capability,
)
from core.agent_mcp_adapter import discover
from core.models import (
    AgentCapabilityInvocation,
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)

HOST = "mcp.example.com"
ENDPOINT = "https://mcp.example.com/rpc"
SECRET = "secret-token-abc123"
_REAL_CLIENT = httpx.Client

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "data": {
            "type": "object",
            "properties": {
                "celsius": {"type": "integer"},
                "conditions": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _manifest():
    return {
        "mcp": {"timeout_seconds": 10},
        "capabilities": [
            {
                "name": "fetch_weather",
                "description": "Fetch the current weather",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 10,
                "remote_name": "fetch-weather",
            }
        ],
    }


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(username="mcp-owner", password="test-password")
    tenant = Tenant.objects.create(name="MCP tenant", slug="mcp-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="MCP project", slug="mcp-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, protocol="mcp", manifest=None, auth_required=True):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="mcp-agent",
        name="MCP Agent",
        version="1.0.0",
        protocol=protocol,
        endpoint_url=ENDPOINT,
        auth_required=auth_required,
        capabilities=["fetch_weather"],
        manifest=manifest or _manifest(),
    )


def _install(tenant, project, plugin, user, *, credential_ref="", granted=None):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status="installed",
        granted_capabilities=granted or ["fetch_weather"],
        credential_ref=credential_ref,
        installed_by=user,
    )


def _rpc(request_id, result):
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8"),
    )


def _make_handler(calls, *, pages=None, call_result=None, call_error=False, status=200):
    def handler(request):
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
            }
        )
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={
                    "Mcp-Session-Id": "sess-1",
                    "content-type": "application/json",
                },
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                        },
                    }
                ).encode("utf-8"),
            )
        if method == "tools/list":
            cursor = body.get("params", {}).get("cursor")
            page = pages[0] if not cursor else pages[1]
            result: dict = {"tools": page["tools"]}
            if page.get("nextCursor"):
                result["nextCursor"] = page["nextCursor"]
            return _rpc(body.get("id"), result)
        if method == "tools/call":
            if status != 200:
                return httpx.Response(status, content=b"{}")
            if call_error:
                return _rpc(
                    body.get("id"),
                    {
                        "content": [{"type": "text", "text": "remote boom"}],
                        "isError": True,
                    },
                )
            return _rpc(
                body.get("id"),
                call_result or {"content": [{"type": "text", "text": "sunny"}], "isError": False},
            )
        # Notifications (for example ``notifications/initialized``) carry no
        # response frame; acknowledge them with an empty 200 JSON body.
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    return handler


def _patch_transport(monkeypatch, handler):
    def factory(**kwargs):
        return _REAL_CLIENT(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.mcp_connector.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


@pytest.mark.django_db
def test_mcp_discover_maps_paged_tools_and_skips_invalid_schema(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    pages = [
        {
            "tools": [
                {
                    "name": "fetch-weather",
                    "description": "Get weather",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
                {
                    "name": "BadSchema",
                    "description": "Array input",
                    "inputSchema": {"type": "array"},
                },
                {
                    "name": "Get Weather!",
                    "description": "Spaced name",
                    "inputSchema": {"type": "object"},
                },
            ],
            "nextCursor": "page2",
        },
        {
            "tools": [
                {
                    "name": "extra_tool",
                    "description": "Second page",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
    ]
    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, pages=pages))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        capabilities = discover(installation)

    by_name = {entry["name"]: entry for entry in capabilities}
    assert set(by_name) == {"fetch-weather", "get_weather", "extra_tool"}
    weather = by_name["fetch-weather"]
    assert weather["description"] == "Get weather"
    assert weather["input_schema"]["additionalProperties"] is False
    assert weather["remote_name"] == "fetch-weather"
    spaced = by_name["get_weather"]
    assert spaced["remote_name"] == "Get Weather!"
    # initialize, initialized notification and two tools/list pages.
    assert len(calls) == 4


@pytest.mark.django_db
def test_mcp_invoke_success_through_router(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    monkeypatch.setenv("PLUGIN_SECRET_MCP_TOKEN", SECRET)
    plugin = _make_plugin(tenant, user)
    installation = _install(
        tenant, project, plugin, user, credential_ref="env://PLUGIN_SECRET_MCP_TOKEN"
    )

    calls: list[dict] = []
    _patch_transport(
        monkeypatch,
        _make_handler(
            calls,
            call_result={"content": [{"type": "text", "text": "sunny"}], "isError": False},
        ),
    )

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id), str(project.id), "fetch_weather", {"city": "Austin"}
        )

    assert result == {"text": "sunny"}
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert record.protocol == "mcp"
    assert record.error_code == ""
    assert calls[0]["headers"].get("authorization") == f"Bearer {SECRET}"


@pytest.mark.django_db
def test_mcp_structured_content_returned_as_data(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    _install(tenant, project, plugin, user)

    structured = {"celsius": 21, "conditions": "clear"}
    calls: list[dict] = []
    _patch_transport(
        monkeypatch,
        _make_handler(
            calls,
            call_result={
                "content": [],
                "isError": False,
                "structuredContent": structured,
            },
        ),
    )

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id), str(project.id), "fetch_weather", {"city": "Austin"}
        )

    assert result == {"data": structured}


@pytest.mark.django_db
def test_mcp_is_error_records_failed_invocation_without_secret(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    monkeypatch.setenv("PLUGIN_SECRET_MCP_TOKEN", SECRET)
    plugin = _make_plugin(tenant, user)
    installation = _install(
        tenant, project, plugin, user, credential_ref="env://PLUGIN_SECRET_MCP_TOKEN"
    )

    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, call_error=True))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(str(tenant.id), str(project.id), "fetch_weather", {"city": "Austin"})

    assert SECRET not in str(exc.value)
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED
    assert not AgentCapabilityInvocation.objects.filter(
        status=AgentCapabilityInvocation.Status.SUCCESS
    ).exists()


@pytest.mark.django_db
def test_mcp_timeout_records_failed_invocation(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    def timeout_handler(request):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(
        "core.mcp_connector.httpx.Client",
        lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(timeout_handler),
            follow_redirects=False,
            trust_env=False,
        ),
    )
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError):
            invoke_capability(str(tenant.id), str(project.id), "fetch_weather", {"city": "Austin"})

    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_discover_endpoint_merges_capabilities_idempotently(workspace, monkeypatch):
    user, tenant, project, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    _install(tenant, project, plugin, user)
    plugin.manifest = {}
    plugin.capabilities = []
    plugin.save(update_fields=["manifest", "capabilities"])

    pages = [
        {
            "tools": [
                {
                    "name": "discovered_tool",
                    "description": "From remote",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
        {"tools": []},
    ]
    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, pages=pages))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        first = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert first.status_code == 200
    assert first.data["protocol"] == "mcp"
    assert first.data["discovered"] == 1

    plugin.refresh_from_db()
    names = [entry["name"] for entry in plugin.manifest["capabilities"]]
    assert "discovered_tool" in names
    assert len(names) == len(set(names))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        second = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert second.status_code == 200
    plugin.refresh_from_db()
    names = [entry["name"] for entry in plugin.manifest["capabilities"]]
    assert len(names) == len(set(names))
    assert "discovered_tool" in names


@pytest.mark.django_db
def test_discover_endpoint_rejects_non_admin(workspace, monkeypatch):
    user, tenant, project, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    outsider = get_user_model().objects.create_user(username="mcp-member", password="test-password")
    TenantMembership.objects.create(tenant=tenant, user=outsider, role=TenantMembership.Role.MEMBER)
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    response = outsider_client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
        format="json",
    )
    assert response.status_code == 403
    plugin.refresh_from_db()
    assert "discovered_tool" not in [entry.get("name") for entry in plugin.manifest["capabilities"]]


@pytest.mark.django_db
def test_discover_endpoint_rejects_unsupported_protocol_without_write(workspace):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user, protocol="http_json", auth_required=False)
    original_manifest = dict(plugin.manifest)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
        format="json",
    )
    assert response.status_code == 400
    plugin.refresh_from_db()
    assert plugin.manifest == original_manifest


@pytest.mark.django_db
def test_discover_endpoint_remote_failure_returns_502_without_write(workspace, monkeypatch):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    original_manifest = dict(plugin.manifest)

    def failure_handler(request):
        return httpx.Response(500, content=b"{}")

    monkeypatch.setattr(
        "core.mcp_connector.httpx.Client",
        lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(failure_handler),
            follow_redirects=False,
            trust_env=False,
        ),
    )
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert response.status_code == 502
    plugin.refresh_from_db()
    assert plugin.manifest == original_manifest


@pytest.mark.django_db
def test_register_rejects_handwritten_mcp_capability_without_discovery(workspace):
    user, tenant, _, client = workspace
    manifest = {
        "capabilities": [
            {
                "name": "fetch_weather",
                "description": "Handwritten capability",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 10,
            }
        ]
    }
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "mcp-handwritten",
            "name": "MCP Handwritten",
            "version": "1.0.0",
            "protocol": "mcp",
            "endpoint_url": ENDPOINT,
            "auth_required": False,
            "manifest": manifest,
        },
        format="json",
    )
    assert response.status_code == 400
    assert "discover" in response.data["detail"].lower()
    assert not AgentPluginDefinition.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_discover_writes_discovery_record_and_marks_capabilities(workspace, monkeypatch):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    plugin.manifest = {}
    plugin.capabilities = []
    plugin.save(update_fields=["manifest", "capabilities"])

    pages = [
        {
            "tools": [
                {
                    "name": "discovered_tool",
                    "description": "From remote",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
        {"tools": []},
    ]
    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, pages=pages))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert response.status_code == 200

    plugin.refresh_from_db()
    record = plugin.manifest["mcp"]["discovery"]
    assert record["endpoint"] == agent_plugin_manifest.endpoint_fingerprint(ENDPOINT)
    assert record["tool_count"] == 1
    assert record["tools"] == ["discovered_tool"]
    entry = plugin.manifest["capabilities"][0]
    assert entry["source"] == agent_plugin_manifest.SOURCE_DISCOVERED
    assert (
        agent_plugin_manifest.validate_manifest(plugin.manifest, "mcp", endpoint_url=ENDPOINT)
        is None
    )


@pytest.mark.django_db
def test_endpoint_change_makes_discovery_record_stale(workspace, monkeypatch):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    plugin.manifest = {}
    plugin.capabilities = []
    plugin.save(update_fields=["manifest", "capabilities"])

    pages = [
        {
            "tools": [
                {
                    "name": "discovered_tool",
                    "description": "From remote",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
        {"tools": []},
    ]
    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, pages=pages))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )

    plugin.refresh_from_db()
    plugin.endpoint_url = "https://other-host.example.com/rpc"
    plugin.save(update_fields=["endpoint_url"])

    error = agent_plugin_manifest.validate_manifest(
        plugin.manifest, "mcp", endpoint_url=plugin.endpoint_url
    )
    assert error is not None
    assert "stale" in error


@pytest.mark.django_db
def test_repeat_discover_is_idempotent(workspace, monkeypatch):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    plugin.manifest = {}
    plugin.capabilities = []
    plugin.save(update_fields=["manifest", "capabilities"])

    pages = [
        {
            "tools": [
                {
                    "name": "discovered_tool",
                    "description": "From remote",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
        {"tools": []},
    ]
    calls: list[dict] = []
    _patch_transport(monkeypatch, _make_handler(calls, pages=pages))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        first = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
        second = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert first.status_code == 200
    assert second.status_code == 200

    plugin.refresh_from_db()
    names = [entry["name"] for entry in plugin.manifest["capabilities"]]
    assert len(names) == len(set(names)) == 1
    record = plugin.manifest["mcp"]["discovery"]
    assert record["tool_count"] == 1
    assert record["tools"] == ["discovered_tool"]
