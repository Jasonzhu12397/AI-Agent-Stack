# ruff: noqa: I001
import json

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from core import agent_plugin_manifest
from core.agent_a2a_adapter import (
    CAPABILITY_CONFIG_INVALID,
    CAPABILITY_STREAMING_UNSUPPORTED,
    CAPABILITY_UPSTREAM_ERROR,
    discover_card,
)
from core.agent_capability_router import (
    CAPABILITY_INVOCATION_FAILED,
    CapabilityResolutionError,
    invoke_capability,
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

HOST = "a2a.example.com"
ENDPOINT = "https://a2a.example.com/agent"
_REAL_CLIENT = httpx.Client

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "stream": {"type": "boolean"},
        "streaming": {"type": "boolean"},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "data": {"type": "object"}},
    "required": [],
    "additionalProperties": False,
}


def _manifest():
    return {
        "a2a": {"agent_card_path": "/.well-known/agent-card.json", "timeout_seconds": 10},
        "capabilities": [
            {
                "name": "greet",
                "description": "Greet a traveler",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 10,
                "remote_name": "greet",
            }
        ],
    }


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(username="a2a-owner", password="test-password")
    tenant = Tenant.objects.create(name="A2A tenant", slug="a2a-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="A2A project", slug="a2a-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, manifest=None, auth_required=False):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="a2a-agent",
        name="A2A Agent",
        version="1.0.0",
        protocol="a2a",
        endpoint_url=ENDPOINT,
        auth_required=auth_required,
        capabilities=["greet"],
        manifest=manifest or _manifest(),
    )


def _install(tenant, project, plugin, user):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status="installed",
        granted_capabilities=["greet"],
        credential_ref="",
        installed_by=user,
    )


def _card(streaming=False):
    return {
        "name": "Travel Agent",
        "description": "A2A travel assistant",
        "capabilities": {"streaming": streaming},
        "skills": [
            {
                "id": "greet",
                "description": "Greet a traveler",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                "outputSchema": {"type": "object"},
            }
        ],
    }


def _patch_transport(monkeypatch, handler):
    def factory(**kwargs):
        return _REAL_CLIENT(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.agent_a2a_adapter.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.36", 443))],
    )


@pytest.mark.django_db
def test_a2a_discover_card_maps_skills(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_card()).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        capabilities = discover_card(installation)

    assert len(capabilities) == 1
    skill = capabilities[0]
    assert skill["name"] == "greet"
    assert skill["description"] == "Greet a traveler"
    assert skill["input_schema"]["additionalProperties"] is False
    assert skill["output_schema"]["additionalProperties"] is False
    assert skill["remote_name"] == "greet"


@pytest.mark.django_db
def test_a2a_card_missing_required_field_rejected(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    card = _card()
    del card["skills"]

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(card).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            discover_card(installation)

    assert exc.value.code == CAPABILITY_CONFIG_INVALID
    assert "skills" in str(exc.value)


@pytest.mark.django_db
def test_a2a_invoke_success_through_router(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    calls: list[dict] = []

    def handler(request):
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"artifacts": [{"text": "Hello, traveler!"}]},
                }
            ).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(str(tenant.id), str(project.id), "greet", {"prompt": "Hi there"})

    assert result == {"text": "Hello, traveler!"}
    body = calls[0]["body"]
    assert body["method"] == "message/send"
    assert body["jsonrpc"] == "2.0"
    assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "Hi there"}]

    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert record.protocol == "a2a"
    assert record.error_code == ""


@pytest.mark.django_db
def test_a2a_json_rpc_error_frame_records_failed_invocation(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "agent busy"},
                }
            ).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(str(tenant.id), str(project.id), "greet", {"prompt": "Hi"})

    assert exc.value.code == CAPABILITY_UPSTREAM_ERROR
    assert "agent busy" in str(exc.value)
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_a2a_message_parts_text_fallback(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"message": {"parts": [{"kind": "text", "text": "reply in parts"}]}},
                }
            ).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(str(tenant.id), str(project.id), "greet", {"prompt": "Hi"})

    assert result == {"text": "reply in parts"}


@pytest.mark.django_db
def test_register_rejects_handwritten_a2a_capability_without_discovery(workspace):
    user, tenant, _, client = workspace
    manifest = {
        "capabilities": [
            {
                "name": "greet",
                "description": "Handwritten skill",
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
            "plugin_key": "a2a-handwritten",
            "name": "A2A Handwritten",
            "version": "1.0.0",
            "protocol": "a2a",
            "endpoint_url": ENDPOINT,
            "auth_required": False,
            "manifest": manifest,
        },
        format="json",
    )
    assert response.status_code == 400
    assert "discover" in response.data["error"]["message"].lower()
    assert not AgentPluginDefinition.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_discover_card_writes_discovery_record_and_marks_skill(workspace, monkeypatch):
    user, tenant, _, client = workspace
    plugin = _make_plugin(tenant, user)
    plugin.manifest = {}
    plugin.capabilities = []
    plugin.save(update_fields=["manifest", "capabilities"])

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_card()).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/agent-plugins/{plugin.id}/discover/",
            format="json",
        )
    assert response.status_code == 200

    plugin.refresh_from_db()
    record = plugin.manifest["a2a"]["discovery"]
    assert record["endpoint"] == agent_plugin_manifest.endpoint_fingerprint(ENDPOINT)
    assert record["tool_count"] == 1
    assert record["tools"] == ["greet"]
    entry = plugin.manifest["capabilities"][0]
    assert entry["source"] == agent_plugin_manifest.SOURCE_DISCOVERED
    assert (
        agent_plugin_manifest.validate_manifest(plugin.manifest, "a2a", endpoint_url=ENDPOINT)
        is None
    )


def _success_stream_handler(calls):
    def handler(request):
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"artifacts": [{"text": "Hello, traveler!"}]},
                }
            ).encode("utf-8"),
        )

    return handler


@pytest.mark.django_db
def test_a2a_streaming_false_rejects_stream_request_without_remote_call(
    workspace, monkeypatch
):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_stream_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "greet",
                {"prompt": "Hi", "stream": True},
            )

    assert exc.value.code == CAPABILITY_STREAMING_UNSUPPORTED
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_a2a_streaming_true_allows_message_send(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    # Persist the discovered capability with the streaming flag enabled.
    plugin.manifest["capabilities"][0]["streaming"] = True
    plugin.save(update_fields=["manifest"])

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_stream_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            "greet",
            {"prompt": "Hi", "stream": True},
        )

    assert result == {"text": "Hello, traveler!"}
    assert len(calls) == 1
    assert calls[0]["body"]["method"] == "message/send"
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS


@pytest.mark.django_db
def test_a2a_missing_streaming_flag_defaults_to_non_streaming(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    _install(tenant, project, plugin, user)
    plugin.manifest["capabilities"][0].pop("streaming", None)
    plugin.save(update_fields=["manifest"])

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_stream_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "greet",
                {"prompt": "Hi", "streaming": True},
            )

    assert exc.value.code == CAPABILITY_STREAMING_UNSUPPORTED
    assert calls == []


@pytest.mark.django_db
def test_a2a_non_boolean_streaming_defaults_to_non_streaming(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    _install(tenant, project, plugin, user)
    plugin.manifest["capabilities"][0]["streaming"] = "yes"
    plugin.save(update_fields=["manifest"])

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_stream_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "greet",
                {"prompt": "Hi", "stream": True},
            )

    assert exc.value.code == CAPABILITY_STREAMING_UNSUPPORTED
    assert calls == []


@pytest.mark.django_db
def test_a2a_discovery_records_streaming_flag(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_card(streaming=True)).encode("utf-8"),
        )

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        capabilities = discover_card(installation)

    assert capabilities[0]["streaming"] is True

    def non_bool_handler(request):
        card = _card()
        card["capabilities"]["streaming"] = "yes"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(card).encode("utf-8"),
        )

    _patch_transport(monkeypatch, non_bool_handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        capabilities = discover_card(installation)

    assert capabilities[0]["streaming"] is False
