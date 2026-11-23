import json
from unittest.mock import patch

import pytest
from core.agent_capability_router import (
    CAPABILITY_DISABLED,
    CAPABILITY_NOT_INSTALLED,
    CAPABILITY_UNINSTALLED_IN_FLIGHT,
    CapabilityResolutionError,
    invoke_capability,
    resolve_capability,
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
from core.tool_router import ToolRouter
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _manifest(capability="summarize", risk_level="low"):
    return {
        "http": {
            "base_url": "https://market-agent.example.com",
            "operations": {
                capability: {
                    "method": "GET",
                    "path": "/v1/" + capability,
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": capability,
                "description": "Summarize input text",
                "input_schema": TEXT_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": risk_level,
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def capability_workspace(db):
    user = get_user_model().objects.create_user(
        username="capability-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Capability tenant", slug="capability-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Capability project", slug="capability-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _make_plugin(tenant, user, *, version="1.0.0", key="market-agent-a", manifest=None):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key,
        name="Market Agent",
        version=version,
        protocol="http_json",
        endpoint_url="https://market-agent.example.com/v1/invoke",
        auth_required=False,
        capabilities=["summarize"],
        manifest=manifest or _manifest(),
    )


def _install(tenant, project, plugin, user, granted=None, status="installed"):
    return AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=plugin,
        plugin_version=plugin.version,
        status=status,
        granted_capabilities=granted or ["summarize"],
        installed_by=user,
    )


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._payload


class FakeClient:
    def __init__(self, response, captured, side_effect=None):
        self.response = response
        self.captured = captured
        self.side_effect = side_effect

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.captured.update({"method": method, "url": url, **kwargs})
        if self.side_effect is not None:
            self.side_effect()
        return self.response


def _mock_http(monkeypatch, captured, payload, side_effect=None):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    return patch(
        "core.http_connector.httpx.Client",
        return_value=FakeClient(FakeResponse(payload), captured, side_effect),
    )


@pytest.mark.django_db
def test_resolve_and_invoke_http_capability_success(capability_workspace, monkeypatch):
    user, tenant, project, _ = capability_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.installation.id == installation.id
    assert resolved.plugin.plugin_key == "market-agent-a"

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "done"}):
            result = invoke_capability(
                str(tenant.id), str(project.id), "summarize", {"text": "hello"}
            )

    assert result == {"summary": "done"}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://market-agent.example.com/v1/summarize"

    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert record.capability == "summarize"
    assert record.protocol == "http_json"
    assert record.error_code == ""
    assert record.latency_ms >= 0
    assert not hasattr(record, "arguments")


@pytest.mark.django_db
def test_resolve_missing_installation_raises_not_installed(capability_workspace):
    user, tenant, project, _ = capability_workspace
    _make_plugin(tenant, user)

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
def test_resolve_disabled_installation_raises_disabled(capability_workspace):
    user, tenant, project, _ = capability_workspace
    plugin = _make_plugin(tenant, user)
    _install(tenant, project, plugin, user, status="disabled")

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_DISABLED


@pytest.mark.django_db
def test_uninstall_makes_capability_unresolvable_and_clears_handlers(
    capability_workspace, monkeypatch
):
    user, tenant, project, _ = capability_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "done"}):
            invoke_capability(
                str(tenant.id), str(project.id), "summarize", {"text": "hello"}
            )

    installation.status = AgentPluginInstallation.Status.UNINSTALLED
    installation.save(update_fields=["status", "updated_at"])

    with pytest.raises(CapabilityResolutionError) as resolve_exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolve_exc.value.code == CAPABILITY_NOT_INSTALLED

    with pytest.raises(CapabilityResolutionError) as invoke_exc:
        invoke_capability(str(tenant.id), str(project.id), "summarize", {"text": "hello"})
    assert invoke_exc.value.code == CAPABILITY_NOT_INSTALLED

    router = ToolRouter()
    router.register(
        plugin.plugin_key,
        "summarize",
        lambda args, ctx: {},
        plugin_version=plugin.version,
    )
    assert router.has_handler(plugin.plugin_key, "summarize", plugin_version=plugin.version)
    first = router.unregister(plugin.plugin_key)
    assert first == 1
    assert not router.has_handler(plugin.plugin_key, "summarize", plugin_version=plugin.version)
    second = router.unregister(plugin.plugin_key)
    assert second == 0


@pytest.mark.django_db
def test_uninstall_status_view_fires_invalidation_hook(capability_workspace):
    from core.agent_capability_router import register_invalidation_hook

    user, tenant, project, client = capability_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    calls: list[str] = []
    register_invalidation_hook(lambda inst: calls.append(str(inst.id)))

    status_url = f"/api/v1/agent-plugin-installations/{installation.id}/status/"
    response = client.post(status_url, {"status": "uninstalled"}, format="json")
    assert response.status_code == 200
    assert calls == [str(installation.id)]

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
def test_invoke_rejects_when_uninstalled_mid_flight(capability_workspace, monkeypatch):
    user, tenant, project, _ = capability_workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    def uninstall_mid_flight():
        installation.status = AgentPluginInstallation.Status.UNINSTALLED
        installation.save(update_fields=["status", "updated_at"])

    captured: dict = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["market-agent.example.com"]):
        with _mock_http(monkeypatch, captured, {"summary": "done"}, uninstall_mid_flight):
            with pytest.raises(CapabilityResolutionError) as exc:
                invoke_capability(
                    str(tenant.id), str(project.id), "summarize", {"text": "hello"}
                )
    assert exc.value.code == CAPABILITY_UNINSTALLED_IN_FLIGHT

    record = AgentCapabilityInvocation.objects.get(installation=installation)
    assert record.status == AgentCapabilityInvocation.Status.FAILED
    assert record.error_code == CAPABILITY_UNINSTALLED_IN_FLIGHT
    assert not AgentCapabilityInvocation.objects.filter(
        status=AgentCapabilityInvocation.Status.SUCCESS
    ).exists()


@pytest.mark.django_db
def test_install_and_uninstall_are_idempotent(capability_workspace):
    user, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "market-agent-a",
            "name": "Market Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": _manifest(),
        },
        format="json",
    )
    assert registered.status_code == 201
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    first = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    second = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert AgentPluginInstallation.objects.filter(project=project).count() == 1

    status_url = f"/api/v1/agent-plugin-installations/{first.data['id']}/status/"
    uninstall = client.post(status_url, {"status": "uninstalled"}, format="json")
    repeat_uninstall = client.post(status_url, {"status": "uninstalled"}, format="json")
    assert uninstall.status_code == 200
    assert repeat_uninstall.status_code == 200
    assert AgentPluginInstallation.objects.filter(project=project).count() == 1


@pytest.mark.django_db
def test_capability_selection_prefers_newer_version_then_newer_install(capability_workspace):
    user, tenant, project, _ = capability_workspace
    older = _make_plugin(tenant, user, version="1.0.0", key="market-agent-old")
    newer = _make_plugin(tenant, user, version="1.2.0", key="market-agent-new")
    _install(tenant, project, older, user)
    _install(tenant, project, newer, user)

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.plugin.version == "1.2.0"

    same_version_a = _make_plugin(tenant, user, version="2.0.0", key="market-agent-a2")
    same_version_b = _make_plugin(tenant, user, version="2.0.0", key="market-agent-b2")
    install_a = _install(tenant, project, same_version_a, user)
    install_b = _install(tenant, project, same_version_b, user)
    install_b.created_at = install_a.created_at
    install_b.save(update_fields=["created_at"])

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    # Equal version and equal created_at: installation id lexicographic order wins.
    expected_id = sorted([install_a.id, install_b.id])[0]
    assert resolved.installation.id == expected_id


@pytest.mark.django_db
def test_capabilities_list_returns_only_active_installations(capability_workspace):
    user, tenant, project, client = capability_workspace
    active = _make_plugin(tenant, user, key="active-agent")
    disabled = _make_plugin(tenant, user, key="disabled-agent")
    _install(tenant, project, active, user)
    _install(tenant, project, disabled, user, status="disabled")

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-capabilities/"
    )
    assert response.status_code == 200
    entries = response.data["capabilities"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "summarize"
    assert entry["plugin_key"] == "active-agent"
    assert entry["plugin_version"] == "1.0.0"
    assert entry["protocol"] == "http_json"
    assert entry["risk_level"] == "low"
    assert entry["installation_id"] == str(AgentPluginInstallation.objects.get(plugin=active).id)
    assert entry["input_schema"] == TEXT_INPUT
    assert entry["last_invocation"] is None


@pytest.mark.django_db
def test_capabilities_list_rejects_non_project_member(capability_workspace):
    _, tenant, project, _ = capability_workspace
    outsider = get_user_model().objects.create_user(
        username="capability-outsider", password="test-password"
    )
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-capabilities/"
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_reinstall_revives_uninstalled_installation(capability_workspace):
    user, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "market-agent-a",
            "name": "Market Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    first = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    status_url = f"/api/v1/agent-plugin-installations/{first.data['id']}/status/"
    client.post(status_url, {"status": "uninstalled"}, format="json")

    revived = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    assert revived.status_code == 200
    assert revived.data["status"] == AgentPluginInstallation.Status.INSTALLED
    assert AgentPluginInstallation.objects.filter(project=project).count() == 1


@pytest.mark.django_db
def test_install_lookup_accepts_plugin_key(capability_workspace):
    _, tenant, project, client = capability_workspace
    client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "market-agent-a",
            "name": "Market Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    by_key = client.post(
        install_url,
        {"plugin_key": "market-agent-a", "granted_capabilities": ["summarize"]},
        format="json",
    )
    assert by_key.status_code == 201
    by_key_version = client.post(
        install_url,
        {
            "plugin_key": "market-agent-a",
            "plugin_version": "1.0.0",
            "granted_capabilities": ["summarize"],
        },
        format="json",
    )
    assert by_key_version.status_code == 200

    missing = client.post(install_url, {"granted_capabilities": ["summarize"]}, format="json")
    assert missing.status_code == 400


@pytest.mark.django_db
def test_disabled_installation_post_leaves_state_unchanged(capability_workspace):
    _, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "market-agent-a",
            "name": "Market Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    first = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    status_url = f"/api/v1/agent-plugin-installations/{first.data['id']}/status/"
    client.post(status_url, {"status": "disabled"}, format="json")

    repeat = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    assert repeat.status_code == 200
    assert repeat.data["status"] == AgentPluginInstallation.Status.DISABLED


@pytest.mark.django_db
def test_auth_required_plugin_reports_readable_credential_error(capability_workspace):
    _, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "secure-agent",
            "name": "Secure Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": True,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    response = client.post(
        install_url,
        {"plugin_id": registered.data["id"], "granted_capabilities": ["summarize"]},
        format="json",
    )
    assert response.status_code == 400
    assert "credential_ref" in response.data["detail"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "credential_ref",
    [
        "env://PLUGIN_SECRET_MARKET_AGENT_TOKEN",
        "vault://tenant/research-agent",
    ],
)
def test_credential_ref_accepts_uri_scheme(capability_workspace, credential_ref):
    _, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "secure-agent",
            "name": "Secure Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": True,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    response = client.post(
        install_url,
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["summarize"],
            "credential_ref": credential_ref,
        },
        format="json",
    )
    assert response.status_code == 201


@pytest.mark.django_db
@pytest.mark.parametrize(
    "credential_ref",
    [
        "sk-live-abc123",
        "Bearer abc",
        "vault://tenant/with space",
        "file:///etc/passwd",
        "vault://" + "a" * 201,
    ],
)
def test_credential_ref_rejects_non_uri(capability_workspace, credential_ref):
    _, tenant, project, client = capability_workspace
    registered = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "secure-agent",
            "name": "Secure Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": True,
            "manifest": _manifest(),
        },
        format="json",
    )
    install_url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/"
    )
    response = client.post(
        install_url,
        {
            "plugin_id": registered.data["id"],
            "granted_capabilities": ["summarize"],
            "credential_ref": credential_ref,
        },
        format="json",
    )
    assert response.status_code == 400
    assert credential_ref not in response.data["detail"]


@pytest.mark.django_db
def test_registration_rejects_invalid_manifest(capability_workspace):
    _, tenant, _, client = capability_workspace
    url = f"/api/v1/tenants/{tenant.id}/agent-plugins/"

    duplicate_names = _manifest()
    duplicate_names["capabilities"].append(
        {
            "name": "summarize",
            "input_schema": TEXT_INPUT,
            "output_schema": SUMMARY_OUTPUT,
            "risk_level": "low",
        }
    )
    response = client.post(
        url,
        {
            "plugin_key": "bad-agent",
            "name": "Bad Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": duplicate_names,
        },
        format="json",
    )
    assert response.status_code == 400
    assert not AgentPluginDefinition.objects.filter(tenant=tenant).exists()

    missing_operation = _manifest()
    missing_operation["http"]["operations"] = {}
    response = client.post(
        url,
        {
            "plugin_key": "bad-agent",
            "name": "Bad Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": missing_operation,
        },
        format="json",
    )
    assert response.status_code == 400

    bad_name = _manifest()
    bad_name["capabilities"][0]["name"] = "Bad Name"
    response = client.post(
        url,
        {
            "plugin_key": "bad-agent",
            "name": "Bad Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://market-agent.example.com/v1/invoke",
            "auth_required": False,
            "manifest": bad_name,
        },
        format="json",
    )
    assert response.status_code == 400
