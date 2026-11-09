import json
from unittest.mock import patch

import pytest
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import ModelConfig, ModelProvider, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.fixture
def model_tenant(db):
    user = get_user_model().objects.create_user(
        username="model-admin",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Model tenant", slug="model-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Local fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "fake model ready"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="fake-chat",
        display_name="Fake Chat",
        capabilities=["chat"],
    )
    return user, tenant, provider, model


@pytest.mark.django_db
def test_fake_model_gateway_returns_normalized_response(model_tenant):
    _, _, _, model = model_tenant

    result = ModelGateway().complete(
        model,
        [{"role": "user", "content": "hello"}],
    )

    assert result.content == "fake model ready"
    assert result.provider == "Local fake"
    assert result.model == "fake-chat"
    assert result.input_tokens == 0


@pytest.mark.django_db
def test_model_provider_and_model_config_api_never_returns_credential_reference(model_tenant):
    user, tenant, provider, _ = model_tenant
    provider.credential_ref = "env:PRIVATE_MODEL_KEY"
    provider.save(update_fields=["credential_ref"])
    client = APIClient()
    client.force_authenticate(user=user)

    providers = client.get(f"/api/v1/tenants/{tenant.id}/model-providers/")
    models = client.get(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider.id}/models/"
    )

    assert providers.status_code == 200
    assert "credential_ref" not in providers.data[0]
    assert providers.data[0]["credential_configured"] is True
    assert models.status_code == 200
    assert models.data[0]["model_name"] == "fake-chat"


@pytest.mark.django_db
def test_tenant_admin_can_create_fake_provider_and_model(model_tenant):
    user, tenant, _, _ = model_tenant
    client = APIClient()
    client.force_authenticate(user=user)

    provider = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": "Test Fake",
            "provider_type": "fake",
            "base_url": "",
            "credential_ref": "",
            "config": {"response": "test response"},
        },
        format="json",
    )
    assert provider.status_code == 201
    model = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider.data['id']}/models/",
        {
            "model_name": "fake-v2",
            "display_name": "Fake V2",
            "capabilities": ["chat", "tools"],
            "context_window": 8192,
            "max_output_tokens": 1024,
        },
        format="json",
    )
    assert model.status_code == 201


@pytest.mark.django_db
def test_openai_provider_requires_https_allowlisted_host_and_env_reference(model_tenant):
    user, tenant, _, _ = model_tenant
    client = APIClient()
    client.force_authenticate(user=user)
    url = f"/api/v1/tenants/{tenant.id}/model-providers/"
    body = {
        "name": "Remote model",
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:MODEL_API_KEY",
        "config": {},
    }

    with override_settings(MODEL_PROVIDER_ALLOWED_HOSTS=["api.example.test"]):
        allowed = client.post(url, body, format="json")
    denied = client.post(url, body, format="json")

    assert allowed.status_code == 201
    assert denied.status_code == 400


def test_openai_compatible_gateway_normalizes_response_and_usage(monkeypatch):
    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "provider answer"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                }
            ).encode("utf-8")

    provider = ModelProvider(
        name="Mock OpenAI",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:MODEL_API_KEY",
    )
    model = ModelConfig(
        provider=provider,
        model_name="test-model",
        display_name="Test Model",
        max_output_tokens=200,
    )
    monkeypatch.setenv("MODEL_API_KEY", "test-secret")
    class FakeOpener:
        request = None

        def open(self, request, timeout):
            self.request = request
            return FakeHTTPResponse()

    fake_opener = FakeOpener()
    with patch("core.model_gateway.build_opener", return_value=fake_opener):
        result = ModelGateway().complete(
            model,
            [{"role": "user", "content": "hello"}],
        )

    assert result.content == "provider answer"
    assert result.input_tokens == 12
    assert result.output_tokens == 7
    assert fake_opener.request.full_url == "https://api.example.test/v1/chat/completions"
    assert fake_opener.request.get_header("Authorization") == "Bearer test-secret"


def test_missing_model_credential_fails_without_revealing_secret():
    provider = ModelProvider(
        name="Missing key",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:NOT_CONFIGURED_MODEL_KEY",
    )
    model = ModelConfig(
        provider=provider,
        model_name="test-model",
        display_name="Test Model",
    )

    with pytest.raises(ModelGatewayError) as exc:
        ModelGateway().complete(model, [{"role": "user", "content": "hello"}])

    assert exc.value.code == "MODEL_CREDENTIAL_MISSING"


@pytest.mark.django_db
def test_model_test_endpoint_executes_fake_model(model_tenant):
    user, tenant, _, model = model_tenant
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-configs/{model.id}/test/",
        {"messages": [{"role": "user", "content": "hello"}]},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["content"] == "fake model ready"
    assert response.data["input_tokens"] == 0


@pytest.mark.django_db
def test_model_config_versions_are_immutable_and_increment(model_tenant):
    user, tenant, provider, model = model_tenant
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider.id}/models/",
        {
            "model_name": model.model_name,
            "display_name": "Fake Chat Updated",
            "capabilities": ["chat"],
            "context_window": 32768,
            "max_output_tokens": 4096,
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["version"] == 2
    assert ModelConfig.objects.filter(provider=provider, model_name=model.model_name).count() == 2


@pytest.mark.django_db
def test_model_provider_rejects_secret_values_inside_config(model_tenant):
    user, tenant, _, _ = model_tenant
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": "Unsafe provider",
            "provider_type": "fake",
            "base_url": "",
            "credential_ref": "",
            "config": {"api_key": "must-not-be-persisted"},
        },
        format="json",
    )

    assert response.status_code == 400
    assert not ModelProvider.objects.filter(tenant=tenant, name="Unsafe provider").exists()
