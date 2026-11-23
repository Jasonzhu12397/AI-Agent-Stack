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
from core.agent_openai_compatible_adapter import (
    CAPABILITY_ARGUMENTS_OUT_OF_RANGE,
    CAPABILITY_UPSTREAM_CLIENT_ERROR,
    CAPABILITY_UPSTREAM_INVALID_RESPONSE,
    CAPABILITY_UPSTREAM_SERVER_ERROR,
    _DEFAULT_MAX_TOKENS_LIMIT,
    _validate_completion_options,
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

HOST = "oai.example.com"
ENDPOINT = "https://oai.example.com/v1/chat/completions"
SECRET = "secret-token-def456"
_REAL_CLIENT = httpx.Client

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        "max_tokens": {"type": "integer"},
        "temperature": {"type": "number"},
    },
    "required": [],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _manifest(*, max_tokens_limit=None):
    block = {
        "base_url": "https://oai.example.com",
        "model": "example-model",
        "path": "/v1/chat/completions",
        "timeout_seconds": 10,
    }
    if max_tokens_limit is not None:
        block["max_tokens_limit"] = max_tokens_limit
    return {
        "openai_compatible": block,
        "capabilities": [
            {
                "name": "complete",
                "description": "Complete a prompt",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 10,
            }
        ],
    }


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(username="oai-owner", password="test-password")
    tenant = Tenant.objects.create(name="OAI tenant", slug="oai-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="OAI project", slug="oai-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, manifest=None, auth_required=True):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="oai-agent",
        name="OAI Agent",
        version="1.0.0",
        protocol="openai_compatible",
        endpoint_url=ENDPOINT,
        auth_required=auth_required,
        capabilities=["complete"],
        manifest=manifest or _manifest(),
    )


def _install(tenant, project, plugin, user, *, credential_ref=""):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status="installed",
        granted_capabilities=["complete"],
        credential_ref=credential_ref,
        installed_by=user,
    )


def _patch_transport(monkeypatch, handler):
    def factory(**kwargs):
        return _REAL_CLIENT(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setattr("core.agent_openai_compatible_adapter.httpx.Client", factory)
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.35", 443))],
    )


def _success_handler(calls, content="hello world"):
    def handler(request):
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8"),
        )

    return handler


@pytest.mark.django_db
def test_openai_invoke_success_through_router(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    monkeypatch.setenv("PLUGIN_SECRET_OAI_TOKEN", SECRET)
    plugin = _make_plugin(tenant, user)
    installation = _install(
        tenant, project, plugin, user, credential_ref="env://PLUGIN_SECRET_OAI_TOKEN"
    )

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id), str(project.id), "complete", {"prompt": "Summarize this"}
        )

    assert result == {"text": "hello world"}
    body = calls[0]["body"]
    assert body["model"] == "example-model"
    assert body["messages"] == [{"role": "user", "content": "Summarize this"}]
    assert calls[0]["headers"].get("authorization") == f"Bearer {SECRET}"

    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert record.protocol == "openai_compatible"
    assert record.error_code == ""


@pytest.mark.django_db
def test_openai_accepts_explicit_messages(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls, "replied"))

    messages = [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hi"},
    ]
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id), str(project.id), "complete", {"messages": messages}
        )

    assert result == {"text": "replied"}
    assert calls[0]["body"]["messages"] == messages


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,expected_code",
    [(400, CAPABILITY_UPSTREAM_CLIENT_ERROR), (503, CAPABILITY_UPSTREAM_SERVER_ERROR)],
)
def test_openai_http_status_errors_normalized(workspace, monkeypatch, status, expected_code):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(status, content=b'{"error": "boom"}')

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(str(tenant.id), str(project.id), "complete", {"prompt": "Hi"})

    assert exc.value.code == expected_code
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_non_json_response_rejected(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(str(tenant.id), str(project.id), "complete", {"prompt": "Hi"})

    assert exc.value.code == CAPABILITY_UPSTREAM_INVALID_RESPONSE
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_missing_choice_rejected(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    _install(tenant, project, plugin, user)

    def handler(request):
        return httpx.Response(200, content=json.dumps({"choices": []}).encode("utf-8"))

    _patch_transport(monkeypatch, handler)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(str(tenant.id), str(project.id), "complete", {"prompt": "Hi"})

    assert exc.value.code == CAPABILITY_UPSTREAM_INVALID_RESPONSE


def _openai_manifest(source):
    return {
        "openai_compatible": {"base_url": "https://oai.example.com", "model": "example-model"},
        "capabilities": [
            {
                "name": "complete",
                "source": source,
                "description": "Complete a prompt",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 10,
            }
        ],
    }


def test_openai_manifest_rejects_discovered_source():
    manifest = _openai_manifest(agent_plugin_manifest.SOURCE_DISCOVERED)
    error = agent_plugin_manifest.validate_manifest(
        manifest, "openai_compatible", endpoint_url=ENDPOINT
    )
    assert error is not None
    assert "discovered" in error


def test_openai_manifest_accepts_declared_source():
    manifest = _openai_manifest(agent_plugin_manifest.SOURCE_DECLARED)
    assert (
        agent_plugin_manifest.validate_manifest(
            manifest, "openai_compatible", endpoint_url=ENDPOINT
        )
        is None
    )


@pytest.mark.django_db
def test_openai_rejects_max_tokens_above_manifest_limit(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(
        tenant, user, manifest=_manifest(max_tokens_limit=100), auth_required=False
    )
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "max_tokens": 200},
            )

    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_rejects_max_tokens_above_default_limit(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "max_tokens": _DEFAULT_MAX_TOKENS_LIMIT + 1},
            )

    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_rejects_non_integer_max_tokens_locally(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "max_tokens": 512.5},
            )

    assert exc.value.code == CAPABILITY_INVOCATION_FAILED
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_rejects_temperature_below_zero(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "temperature": -0.5},
            )

    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_rejects_temperature_above_two(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "temperature": 2.5},
            )

    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_rejects_non_number_temperature_locally(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    installation = _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        with pytest.raises(CapabilityResolutionError) as exc:
            invoke_capability(
                str(tenant.id),
                str(project.id),
                "complete",
                {"prompt": "Hi", "temperature": "0.7"},
            )

    assert exc.value.code == CAPABILITY_INVOCATION_FAILED
    assert calls == []
    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_INVOCATION_FAILED


@pytest.mark.django_db
def test_openai_preserves_valid_completion_options(workspace, monkeypatch):
    user, tenant, project, _ = workspace
    plugin = _make_plugin(tenant, user, auth_required=False)
    _install(tenant, project, plugin, user)

    calls: list[dict] = []
    _patch_transport(monkeypatch, _success_handler(calls, "replied"))

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=[HOST]):
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            "complete",
            {"prompt": "Hi", "max_tokens": 512, "temperature": 0.7},
        )

    assert result == {"text": "replied"}
    body = calls[0]["body"]
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.7


def test_openai_options_helper_rejects_non_integer_and_non_number():
    with pytest.raises(CapabilityResolutionError) as exc:
        _validate_completion_options({"max_tokens": True}, {})
    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE

    with pytest.raises(CapabilityResolutionError) as exc:
        _validate_completion_options({"max_tokens": 4.2}, {})
    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE

    with pytest.raises(CapabilityResolutionError) as exc:
        _validate_completion_options({"max_tokens": -5}, {})
    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE

    with pytest.raises(CapabilityResolutionError) as exc:
        _validate_completion_options({"temperature": False}, {})
    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE

    with pytest.raises(CapabilityResolutionError) as exc:
        _validate_completion_options({"temperature": "0.7"}, {})
    assert exc.value.code == CAPABILITY_ARGUMENTS_OUT_OF_RANGE


def test_openai_options_helper_omits_absent_options():
    assert _validate_completion_options({"prompt": "Hi"}, {}) == {}
    options = _validate_completion_options(
        {"max_tokens": 64, "temperature": 0.2}, {}
    )
    assert options == {"max_tokens": 64, "temperature": 0.2}
