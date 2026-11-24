"""A signed-in member chats on the workspace key; no per-user API key exists."""

import pytest
from core.model_gateway import ModelGatewayError, resolve_credential
from core.models import (
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.provider_accounts import (
    platform_credential_reference,
    platform_env_configured,
    sync_platform_providers,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

PLATFORM_ENV = "PLATFORM_OPENAI_KEY"


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(username="member", password="member-pass")
    tenant = Tenant.objects.create(name="Pool tenant", slug="pool-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Pool project", slug="pool-project")
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    return user, tenant, project


def test_workspace_key_creates_one_shared_provider(workspace, monkeypatch):
    user, tenant, _ = workspace
    monkeypatch.setenv(PLATFORM_ENV, "sk-workspace")
    created = sync_platform_providers(tenant=tenant, user=user)
    assert len(created) == 1
    provider = created[0]
    assert provider.auth_mode == ModelProvider.AuthMode.PLATFORM_POOL
    assert provider.preset_key == "openai"
    assert provider.credential_ref == "env://" + PLATFORM_ENV
    assert provider.name.startswith("OpenAI (platform)")
    names = set(
        ModelConfig.objects.filter(provider=provider).values_list("model_name", flat=True)
    )
    assert names == {"gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"}
    # Running the sync again must not duplicate the row or its models.
    again = sync_platform_providers(tenant=tenant, user=user)
    assert [item.id for item in again] == [provider.id]
    assert ModelConfig.objects.filter(provider=provider).count() == 3


def test_no_workspace_key_means_no_phantom_provider(workspace, monkeypatch):
    user, tenant, _ = workspace
    monkeypatch.delenv(PLATFORM_ENV, raising=False)
    assert sync_platform_providers(tenant=tenant, user=user) == []
    assert not ModelProvider.objects.filter(
        tenant=tenant, auth_mode=ModelProvider.AuthMode.PLATFORM_POOL
    ).exists()


def test_chat_context_marks_shared_models_ready_without_a_link(workspace, monkeypatch):
    user, tenant, project = workspace
    monkeypatch.setenv(PLATFORM_ENV, "sk-workspace")
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"
    )
    assert response.status_code == 200
    models = response.data["models"]
    shared = [item for item in models if item["auth_mode"] == "platform_pool"]
    assert shared, "the workspace provider was not offered to the chat"
    assert all(item["ready"] is True for item in shared)
    assert all(item["account"] is None for item in shared)


def test_missing_workspace_key_names_the_variable(workspace, monkeypatch):
    user, tenant, _ = workspace
    monkeypatch.delenv(PLATFORM_ENV, raising=False)
    assert platform_credential_reference("openai") == "env://" + PLATFORM_ENV
    assert platform_env_configured("openai") is False
    with pytest.raises(ModelGatewayError, match=PLATFORM_ENV):
        resolve_credential(platform_credential_reference("openai"))


def test_workspace_key_resolves_for_any_member(workspace, monkeypatch):
    """The gateway reads the operator key directly; there is no member lookup."""

    monkeypatch.setenv(PLATFORM_ENV, "sk-workspace")
    user, _, _ = workspace
    assert resolve_credential("env://" + PLATFORM_ENV, user) == "sk-workspace"
