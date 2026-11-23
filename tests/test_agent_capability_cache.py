"""Tests for the revision-keyed process-local capability cache.

These cover cache hit/miss accounting, cross-process revision invalidation,
immediate uninstall/disable visibility, installation save() bump semantics,
definition-driven invalidation and deterministic repeat resolution.
"""

import pytest
from core import agent_capability_cache
from core.agent_capability_router import (
    CAPABILITY_DISABLED,
    CAPABILITY_NOT_INSTALLED,
    CapabilityResolutionError,
    resolve_capability,
)
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import F
from django.test.utils import CaptureQueriesContext

TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def _manifest(capability="summarize"):
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
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture(autouse=True)
def _isolated_cache():
    agent_capability_cache.clear()
    yield
    agent_capability_cache.clear()


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(
        username="cache-owner", password="test-password"
    )
    tenant = Tenant.objects.create(name="Cache tenant", slug="cache-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Cache project", slug="cache-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    return user, tenant, project


def _make_plugin(tenant, user, *, version="1.0.0", key="cache-agent", manifest=None):
    return AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key=key,
        name="Cache Agent",
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


@pytest.mark.django_db
def test_protocol_choices_include_openai_compatible():
    choices = dict(AgentPluginDefinition.Protocol.choices)
    assert choices["openai_compatible"] == "OpenAI Compatible"


@pytest.mark.django_db
def test_cache_hit_does_not_re_resolve(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    with CaptureQueriesContext(connection) as first_context:
        first = resolve_capability(str(tenant.id), str(project.id), "summarize")
    stats = agent_capability_cache.cache_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0

    with CaptureQueriesContext(connection) as second_context:
        second = resolve_capability(str(tenant.id), str(project.id), "summarize")

    stats = agent_capability_cache.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert second.installation.id == first.installation.id == installation.id
    # The verified hit path runs one lightweight verification query at most;
    # it must not re-run the full uncached resolution query set.
    assert len(second_context.captured_queries) <= len(first_context.captured_queries)
    assert len(second_context.captured_queries) <= 2


@pytest.mark.django_db
def test_remote_revision_bump_invalidates_cache(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    first = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert first.installation.id == installation.id

    # Simulate another worker process mutating the row directly, bypassing the
    # model save() hook: revision moves on without this process knowing.
    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        revision=F("revision") + 1
    )
    installation.refresh_from_db()
    assert installation.revision == 2

    second = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert second.installation.id == installation.id
    entry = agent_capability_cache.lookup(
        str(tenant.id), str(project.id), "summarize"
    )
    assert entry["installation_revision"] == 2
    stats = agent_capability_cache.cache_stats()
    # The stale entry was rejected and recomputed; the store reflects new state.
    assert stats["size"] == 1


@pytest.mark.django_db
def test_uninstall_is_visible_immediately_without_ttl(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    resolve_capability(str(tenant.id), str(project.id), "summarize")

    # Direct queryset update stands in for another process uninstalling.
    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        status=AgentPluginInstallation.Status.UNINSTALLED
    )

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_NOT_INSTALLED


@pytest.mark.django_db
def test_cross_process_uninstall_is_visible_immediately(workspace):
    """An external-process uninstall must reject the warm local cache at once.

    The queryset update stands in for another worker process writing the row
    directly: it flips the status and bumps the revision in one external write,
    bypassing this process's model save() hook and in-process invalidation hooks.
    Revision/state verification on the next resolve rejects the stale entry
    immediately; the revision comparison guarantees cross-process visibility
    with no TTL delay.
    """
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    warm = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert warm.installation.id == installation.id
    assert agent_capability_cache.cache_stats()["size"] == 1

    # External process write: uninstalled status plus a revision bump, performed
    # without any local invalidation hook running.
    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        status=AgentPluginInstallation.Status.UNINSTALLED,
        revision=F("revision") + 1,
    )

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_NOT_INSTALLED

    # The stale entry was dropped and the failed resolution is not cached.
    assert agent_capability_cache.cache_stats()["size"] == 0


@pytest.mark.django_db
def test_disable_is_visible_immediately_without_ttl(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    resolve_capability(str(tenant.id), str(project.id), "summarize")

    AgentPluginInstallation.objects.filter(pk=installation.pk).update(
        status=AgentPluginInstallation.Status.DISABLED
    )

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_DISABLED


@pytest.mark.django_db
def test_definition_disable_invalidates_installation_cache(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    resolve_capability(str(tenant.id), str(project.id), "summarize")

    plugin.enabled = False
    plugin.save(update_fields=["enabled", "updated_at"])
    installation.refresh_from_db()
    assert installation.revision == 2

    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert exc.value.code == CAPABILITY_DISABLED


@pytest.mark.django_db
def test_save_bumps_revision_on_every_mutation(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)
    assert installation.revision == 1

    installation.status = AgentPluginInstallation.Status.DISABLED
    installation.save()
    assert installation.revision == 2

    installation.status = AgentPluginInstallation.Status.INSTALLED
    installation.save(update_fields=["status", "updated_at"])
    assert installation.revision == 3

    installation.granted_capabilities = ["summarize", "extra"]
    installation.save(update_fields=["granted_capabilities", "updated_at"])
    assert installation.revision == 4

    installation.credential_ref = "vault://tenant/cache-agent"
    installation.save(update_fields=["credential_ref", "updated_at"])
    assert installation.revision == 5

    installation.plugin_version = "1.1.0"
    installation.save(update_fields=["plugin_version", "updated_at"])
    assert installation.revision == 6


@pytest.mark.django_db
def test_definition_manifest_change_bumps_installation_revision(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    resolve_capability(str(tenant.id), str(project.id), "summarize")

    new_manifest = _manifest()
    new_manifest["capabilities"][0]["risk_level"] = "medium"
    plugin.manifest = new_manifest
    plugin.save(update_fields=["manifest", "updated_at"])

    installation.refresh_from_db()
    assert installation.revision == 2

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.installation.id == installation.id
    entry = agent_capability_cache.lookup(
        str(tenant.id), str(project.id), "summarize"
    )
    assert entry["installation_revision"] == 2


@pytest.mark.django_db
def test_definition_version_change_bumps_installation_revision(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user, version="1.0.0")
    installation = _install(tenant, project, plugin, user)

    resolve_capability(str(tenant.id), str(project.id), "summarize")

    plugin.version = "1.2.0"
    plugin.save(update_fields=["version", "updated_at"])

    installation.refresh_from_db()
    assert installation.revision == 2

    resolved = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert resolved.plugin.version == "1.2.0"


@pytest.mark.django_db
def test_cache_clear_forces_database_re_resolution(workspace):
    user, tenant, project = workspace
    plugin = _make_plugin(tenant, user)
    installation = _install(tenant, project, plugin, user)

    first = resolve_capability(str(tenant.id), str(project.id), "summarize")
    agent_capability_cache.clear()
    stats = agent_capability_cache.cache_stats()
    assert stats["size"] == 0
    assert stats["hits"] == 0 and stats["misses"] == 0

    second = resolve_capability(str(tenant.id), str(project.id), "summarize")
    assert second.installation.id == first.installation.id == installation.id


@pytest.mark.django_db
def test_repeated_resolution_keeps_deterministic_winner(workspace):
    user, tenant, project = workspace
    older = _make_plugin(tenant, user, version="1.0.0", key="cache-old")
    newer = _make_plugin(tenant, user, version="1.2.0", key="cache-new")
    _install(tenant, project, older, user)
    _install(tenant, project, newer, user)

    first = resolve_capability(str(tenant.id), str(project.id), "summarize")
    second = resolve_capability(str(tenant.id), str(project.id), "summarize")
    third = resolve_capability(str(tenant.id), str(project.id), "summarize")

    assert first.installation.id == second.installation.id == third.installation.id
    assert first.plugin.version == "1.2.0"
