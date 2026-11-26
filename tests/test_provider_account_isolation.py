"""Personal model platforms stay private to the member who linked them.

Two members can belong to the same tenant and connect different platforms.
Each one must see only their own account-backed providers and models; a
request that targets the other member's provider or model must look like a
missing object instead of exposing its existence.
"""

import pytest
from core.models import (
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def two_members():
    user_model = get_user_model()
    user_a = user_model.objects.create_user(username="isolation-a")
    user_b = user_model.objects.create_user(username="isolation-b")
    tenant = Tenant.objects.create(name="Isolation tenant", slug="isolation-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user_a, role="member")
    TenantMembership.objects.create(tenant=tenant, user=user_b, role="member")
    project = Project.objects.create(
        tenant=tenant, name="Isolation project", slug="isolation-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user_a, role=ProjectMembership.Role.VIEWER
    )
    ProjectMembership.objects.create(
        project=project, user=user_b, role=ProjectMembership.Role.VIEWER
    )
    client_a = APIClient()
    client_a.force_authenticate(user=user_a)
    client_b = APIClient()
    client_b.force_authenticate(user=user_b)
    return user_a, user_b, tenant, project, client_a, client_b


def _create_account_provider(client, tenant, name, preset_key):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": name,
            "provider_type": "openai_compatible",
            "auth_mode": "user_account",
            "preset_key": preset_key,
            "config": {},
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    provider = ModelProvider.objects.get(pk=response.data["id"])
    account = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider.id}/account/",
        {"credential_ref": f"env:{preset_key.upper()}_TEST_KEY", "account_label": name},
        format="json",
    )
    assert account.status_code == 201, account.data
    return provider


@pytest.mark.django_db
def test_each_member_only_sees_their_own_platform_accounts(two_members):
    user_a, user_b, tenant, project, client_a, client_b = two_members
    provider_a_qwen = _create_account_provider(client_a, tenant, "A Qwen", "qwen")
    _provider_a_zhipu = _create_account_provider(client_a, tenant, "A Zhipu", "zhipu")
    _provider_b_openai = _create_account_provider(client_b, tenant, "B OpenAI", "openai")
    _provider_b_google = _create_account_provider(client_b, tenant, "B Google", "google")

    shared = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user_a,
        name="Tenant shared",
        # The shared model is a real remote provider: the composer hides the
        # local echo model once any real model is configured.
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.TENANT_SECRET,
        base_url="https://shared.example.com/v1",
        credential_ref="env:SHARED_TEST_KEY",
    )
    ModelConfig.objects.create(
        provider=shared,
        model_name="shared-chat",
        display_name="Shared chat",
        capabilities=["chat"],
    )

    response_a = client_a.get(f"/api/v1/tenants/{tenant.id}/model-providers/")
    assert response_a.status_code == 200
    rows_a = response_a.data
    account_keys_a = {
        row["preset_key"] for row in rows_a if row["auth_mode"] == "user_account"
    }
    assert account_keys_a == {"qwen", "zhipu"}
    assert "A Qwen" in {row["name"] for row in rows_a}
    assert "B OpenAI" not in {row["name"] for row in rows_a}
    assert "B Google" not in {row["name"] for row in rows_a}
    assert any(row["auth_mode"] == "tenant_secret" for row in rows_a)

    response_b = client_b.get(f"/api/v1/tenants/{tenant.id}/model-providers/")
    assert response_b.status_code == 200
    account_keys_b = {
        row["preset_key"] for row in response_b.data if row["auth_mode"] == "user_account"
    }
    assert account_keys_b == {"openai", "google"}
    assert "B OpenAI" in {row["name"] for row in response_b.data}
    assert "A Qwen" not in {row["name"] for row in response_b.data}
    assert any(row["auth_mode"] == "tenant_secret" for row in response_b.data)

    # The chat composer feeds its model picker from this endpoint, so leaking
    # here would have made the other member's models selectable.
    context_a = client_a.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"
    )
    assert context_a.status_code == 200
    models_a = context_a.data["models"]
    assert all(item["provider_name"] != "B OpenAI" for item in models_a)
    assert all(item["provider_name"] != "B Google" for item in models_a)
    assert any(item["provider_name"] == "A Qwen" for item in models_a)
    assert any(item["provider_name"] == "Tenant shared" for item in models_a)

    context_b = client_b.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"
    )
    assert context_b.status_code == 200
    assert all(item["provider_name"] != "A Qwen" for item in context_b.data["models"])
    assert all(item["provider_name"] != "A Zhipu" for item in context_b.data["models"])
    assert any(item["provider_name"] == "B OpenAI" for item in context_b.data["models"])

    # Foreign providers look absent from every direct provider/model route.
    assert (
        client_a.get(
            f"/api/v1/tenants/{tenant.id}/model-providers/{_provider_b_openai.id}/account/"
        ).status_code
        == 404
    )
    assert (
        client_a.post(
            f"/api/v1/tenants/{tenant.id}/model-providers/{_provider_b_openai.id}/account/",
            {"credential_ref": "env:HIJACK", "account_label": "hijack"},
            format="json",
        ).status_code
        == 404
    )
    assert (
        client_a.get(
            f"/api/v1/tenants/{tenant.id}/model-providers/{_provider_b_openai.id}/models/"
        ).status_code
        == 404
    )

    foreign_model = ModelConfig.objects.filter(
        provider=_provider_b_google
    ).first()
    assert foreign_model is not None
    assert (
        client_a.post(
            f"/api/v1/tenants/{tenant.id}/model-configs/{foreign_model.id}/test/",
            {"messages": [{"role": "user", "content": "hi"}]},
            format="json",
        ).status_code
        == 404
    )
    assert (
        client_a.post(
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/threads/",
            {"title": "Foreign model", "model_config_id": str(foreign_model.id)},
            format="json",
        ).status_code
        == 404
    )

    # The owner still sees their own provider and can open its model list.
    own_model = ModelConfig.objects.filter(provider=provider_a_qwen).first()
    assert own_model is not None
    own_list = client_a.get(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider_a_qwen.id}/models/"
    )
    assert own_list.status_code == 200
    assert str(own_model.id) in {row["id"] for row in own_list.data}
