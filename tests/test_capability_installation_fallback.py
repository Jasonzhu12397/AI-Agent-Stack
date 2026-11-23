# ruff: noqa: I001
"""Multi-installation same-capability selection and fallback tests.

One capability name can be declared by two different plugins and installed
into the same tenant/project under distinct installation ids. These tests pin
the deterministic selection rule, verify that uninstalling the selected
installation falls back to the still-installed survivor (instead of raising
``CAPABILITY_NOT_INSTALLED``), and confirm the process-local cache reflects
that fallback on the next resolve with no TTL delay.

The fallback behavior is router-layer and protocol-independent; the fixtures
therefore use the ``http_json`` protocol with an in-process stub client.
"""

import json
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from core import agent_capability_cache
from core.agent_capability_router import (
    CAPABILITY_NOT_INSTALLED,
    CapabilityResolutionError,
    invoke_capability,
    invalidate_installation,
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

CAPABILITY_NAME = "complete"

INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": [],
    "additionalProperties": False,
}

WINNER_BASE_URL = "https://winner.example.com"
SURVIVOR_BASE_URL = "https://survivor.example.com"


def _manifest(base_url: str) -> dict[str, object]:
    return {
        "http": {
            "base_url": base_url,
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
                "description": "Complete a prompt",
                "input_schema": INPUT_SCHEMA,
                "output_schema": OUTPUT_SCHEMA,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def fallback_workspace(db):
    agent = get_user_model().objects.create_user(
        username="fallback-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Fallback tenant", slug="fallback-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=agent, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Fallback project", slug="fallback-project"
    )
    ProjectMembership.objects.create(
        project=project, user=agent, role=ProjectMembership.Role.ADMIN
    )
    agent_capability_cache.clear()
    return agent, tenant, project


def _make_plugin(
    tenant: Any,
    user: Any,
    *,
    version: str,
    plugin_key: str,
    base_url: str,
) -> AgentPluginDefinition:
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=plugin_key,
        name=plugin_key,
        version=version,
        protocol="http_json",
        endpoint_url=base_url + "/v1/invoke",
        auth_required=False,
        capabilities=[CAPABILITY_NAME],
        manifest=_manifest(base_url),
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


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_bytes(self):
        yield self._payload


class _FakeClient:
    def __init__(
        self, response: _FakeResponse, captured: dict[str, object]
    ) -> None:
        self.response = response
        self.captured = captured

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.captured.update({"method": method, "url": url})
        return self.response


def _stub_survivor(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.37", 443))],
    )
    monkeypatch.setattr(
        "core.http_connector.httpx.Client",
        lambda **kwargs: _FakeClient(_FakeResponse({"text": "survivor reply"}), captured),
    )


def _uninstall(installation: AgentPluginInstallation) -> None:
    installation.status = AgentPluginInstallation.Status.UNINSTALLED
    installation.save(update_fields=["status", "updated_at"])
    # Mirror the status view: drop cached resolutions bound to this installation.
    invalidate_installation(installation)


# ---------------------------------------------------------------------------
# Deterministic selection
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_selection_is_deterministic_and_prefers_newer_version(fallback_workspace):
    user, tenant, project = fallback_workspace
    newer = _make_plugin(
        tenant, user, version="2.0.0", plugin_key="fallback-newer", base_url=WINNER_BASE_URL
    )
    older = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-older", base_url=SURVIVOR_BASE_URL
    )
    newer_install = _install(tenant, project, newer, user)
    older_install = _install(tenant, project, older, user)

    first = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    second = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)

    # Newer semver wins; repeated resolves do not drift between installations.
    assert first.installation.id == newer_install.id
    assert second.installation.id == newer_install.id
    assert first.installation.id == second.installation.id
    assert {newer_install.id, older_install.id} == {
        newer_install.id,
        older_install.id,
    }


@pytest.mark.django_db
def test_tie_break_uses_installation_id_when_version_and_created_at_match(
    fallback_workspace,
):
    user, tenant, project = fallback_workspace
    plugin_a = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-a", base_url=WINNER_BASE_URL
    )
    plugin_b = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-b", base_url=SURVIVOR_BASE_URL
    )
    install_a = _install(tenant, project, plugin_a, user)
    install_b = _install(tenant, project, plugin_b, user)
    install_b.created_at = install_a.created_at
    install_b.save(update_fields=["created_at"])

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)

    # Equal version and equal created_at: installation id lexicographic order wins.
    expected_id = sorted([install_a.id, install_b.id])[0]
    assert resolved.installation.id == expected_id


# ---------------------------------------------------------------------------
# Fallback after uninstall
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_uninstall_winner_falls_back_to_surviving_installation(fallback_workspace):
    user, tenant, project = fallback_workspace
    newer = _make_plugin(
        tenant, user, version="2.0.0", plugin_key="fallback-newer", base_url=WINNER_BASE_URL
    )
    older = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-older", base_url=SURVIVOR_BASE_URL
    )
    winner_install = _install(tenant, project, newer, user)
    survivor_install = _install(tenant, project, older, user)

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert resolved.installation.id == winner_install.id

    _uninstall(winner_install)

    fallback = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert fallback.installation.id == survivor_install.id
    assert fallback.installation.id != winner_install.id


@pytest.mark.django_db
def test_fallback_invocation_routes_to_surviving_adapter(
    fallback_workspace, monkeypatch
):
    user, tenant, project = fallback_workspace
    newer = _make_plugin(
        tenant, user, version="2.0.0", plugin_key="fallback-newer", base_url=WINNER_BASE_URL
    )
    older = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-older", base_url=SURVIVOR_BASE_URL
    )
    winner_install = _install(tenant, project, newer, user)
    survivor_install = _install(tenant, project, older, user)

    resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    _uninstall(winner_install)

    captured: dict[str, object] = {}
    _stub_survivor(monkeypatch, captured)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["survivor.example.com"]):
        result = invoke_capability(
            str(tenant.id),
            str(project.id),
            CAPABILITY_NAME,
            {"text": "hello"},
        )

    # The call reached the surviving plugin's endpoint, not the uninstalled winner.
    assert result == {"text": "survivor reply"}
    assert str(captured["url"]).startswith(SURVIVOR_BASE_URL)
    assert WINNER_BASE_URL not in str(captured["url"])

    record = AgentCapabilityInvocation.objects.get(installation=survivor_install)
    assert record.capability == CAPABILITY_NAME
    assert record.status == AgentCapabilityInvocation.Status.SUCCESS
    assert not AgentCapabilityInvocation.objects.filter(installation=winner_install).exists()


@pytest.mark.django_db
def test_both_uninstalled_raises_not_installed(fallback_workspace):
    user, tenant, project = fallback_workspace
    newer = _make_plugin(
        tenant, user, version="2.0.0", plugin_key="fallback-newer", base_url=WINNER_BASE_URL
    )
    older = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-older", base_url=SURVIVOR_BASE_URL
    )
    winner_install = _install(tenant, project, newer, user, status="uninstalled")
    survivor_install = _install(tenant, project, older, user, status="uninstalled")

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert exc.value.code == CAPABILITY_NOT_INSTALLED
    assert winner_install.id != survivor_install.id


@pytest.mark.django_db
def test_one_installed_one_disabled_resolves_to_installed_survivor(fallback_workspace):
    user, tenant, project = fallback_workspace
    active = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-active", base_url=WINNER_BASE_URL
    )
    disabled = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-disabled", base_url=SURVIVOR_BASE_URL
    )
    active_install = _install(tenant, project, active, user, status="installed")
    _install(tenant, project, disabled, user, status="disabled")

    resolved = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    # Existing router semantics: the active filter skips the disabled installation
    # and resolves the still-installed survivor instead of raising DISABLED.
    assert resolved.installation.id == active_install.id


# ---------------------------------------------------------------------------
# Cache: fallback is immediate, with no TTL delay
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cache_fallback_is_immediate_after_winner_uninstall(fallback_workspace):
    user, tenant, project = fallback_workspace
    newer = _make_plugin(
        tenant, user, version="2.0.0", plugin_key="fallback-newer", base_url=WINNER_BASE_URL
    )
    older = _make_plugin(
        tenant, user, version="1.0.0", plugin_key="fallback-older", base_url=SURVIVOR_BASE_URL
    )
    winner_install = _install(tenant, project, newer, user)
    survivor_install = _install(tenant, project, older, user)

    # Warm the process-local cache against the winner installation.
    warm = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert warm.installation.id == winner_install.id
    cached = agent_capability_cache.lookup(
        str(tenant.id), str(project.id), CAPABILITY_NAME
    )
    assert cached is not None
    assert cached["installation_id"] == str(winner_install.id)

    _uninstall(winner_install)

    # No explicit cache clear and no TTL wait: the next resolve falls back at once.
    fallback = resolve_capability(str(tenant.id), str(project.id), CAPABILITY_NAME)
    assert fallback.installation.id == survivor_install.id

    refreshed = agent_capability_cache.lookup(
        str(tenant.id), str(project.id), CAPABILITY_NAME
    )
    assert refreshed is not None
    assert refreshed["installation_id"] == str(survivor_install.id)
