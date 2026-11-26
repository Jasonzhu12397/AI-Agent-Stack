import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import ModelConfig, ModelProvider, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient


class _StubGuard:
    authorized = True

    def __init__(self):
        self.reservations = []
        self.settlements = []
        self.releases = []

    def reserve(self, *, idempotency_key, requested_tokens, **kwargs):
        reservation = SimpleNamespace(
            reserved_tokens=requested_tokens,
            idempotency_key=idempotency_key,
        )
        self.reservations.append(reservation)
        return reservation

    def settle(self, *, reservation, consumed_tokens):
        reservation.consumed_tokens = consumed_tokens
        self.settlements.append((reservation.idempotency_key, consumed_tokens))

    def release(self, *, reservation):
        reservation.released = True
        self.releases.append(reservation.idempotency_key)


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

    result = ModelGateway(guard=_StubGuard()).complete(
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
        result = ModelGateway(guard=_StubGuard()).complete(
            model,
            [{"role": "user", "content": "hello"}],
        )

    assert result.content == "provider answer"
    assert result.input_tokens == 12
    assert result.output_tokens == 7
    assert fake_opener.request.full_url == "https://api.example.test/v1/chat/completions"
    assert fake_opener.request.get_header("Authorization") == "Bearer test-secret"


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": -1},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": False},
        {"prompt_tokens": 1, "completion_tokens": 201},
        {"prompt_tokens": 100000, "completion_tokens": 1},
        {"prompt_tokens": "12", "completion_tokens": 1},
    ],
)
def test_model_gateway_rejects_invalid_or_unbounded_provider_token_usage(
    monkeypatch, usage
):
    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            return json.dumps({
                "choices": [{"message": {"content": "answer"}}],
                "usage": usage,
            }).encode("utf-8")

    class FakeOpener:
        def open(self, request, timeout):
            return FakeHTTPResponse()

    provider = ModelProvider(
        name="Usage validation provider",
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
    with patch("core.model_gateway.build_opener", return_value=FakeOpener()):
        with pytest.raises(ModelGatewayError) as error:
            ModelGateway(guard=_StubGuard()).complete(
                model,
                [{"role": "user", "content": "hello"}],
                max_tokens=200,
            )
    assert error.value.code == "MODEL_PROVIDER_INVALID_RESPONSE"


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
        ModelGateway(guard=_StubGuard()).complete(model, [{"role": "user", "content": "hello"}])

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


def test_model_gateway_rejects_context_overflow_before_network_call(monkeypatch):
    provider = ModelProvider(
        name="Context limited provider",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:MODEL_API_KEY",
    )
    model = ModelConfig(
        provider=provider,
        model_name="small-context-model",
        display_name="Small Context Model",
        context_window=128,
        max_output_tokens=64,
    )
    monkeypatch.setenv("MODEL_API_KEY", "test-secret")

    with patch("core.model_gateway.build_opener") as opener:
        with pytest.raises(ModelGatewayError) as error:
            ModelGateway(guard=_StubGuard()).complete(
                model,
                [{"role": "user", "content": "x" * 512}],
            )

    assert error.value.code == "MODEL_CONTEXT_WINDOW_EXCEEDED"
    opener.assert_not_called()


def test_model_gateway_rejects_oversized_serialized_tool_context():
    provider = ModelProvider(
        name="Context provider",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "unused"},
    )
    model = ModelConfig(
        provider=provider,
        model_name="fake",
        display_name="Fake",
        capabilities=["chat", "tools"],
        context_window=500000,
        max_output_tokens=64,
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "large_tool",
            "description": "x" * (256 * 1024),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }]

    with pytest.raises(ModelGatewayError) as error:
        ModelGateway(guard=_StubGuard()).complete(
            model,
            [{"role": "user", "content": "hello"}],
            tools=tools,
        )

    assert error.value.code == "MODEL_REQUEST_CONTEXT_TOO_LARGE"


def test_embedding_gateway_rejects_aggregate_input_over_byte_budget():
    provider = ModelProvider(
        name="Embedding provider",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:MODEL_API_KEY",
    )
    model = ModelConfig(
        provider=provider,
        model_name="embedding-model",
        display_name="Embedding Model",
        capabilities=["embeddings"],
    )
    inputs = ["x" * 19000 for _ in range(14)]

    with pytest.raises(ModelGatewayError) as error:
        ModelGateway(guard=_StubGuard()).embed(model, inputs)

    assert error.value.code == "EMBEDDING_INPUT_LIMIT"


def test_fake_model_reads_the_intent_of_the_instruction():
    from core.model_gateway import _intent_tool

    offered = ["weather.current", "map.route.plan", "github.issue.read"]
    assert _intent_tool(offered, "查一下西安天气") == "weather.current"
    assert _intent_tool(offered, "西安到咸阳怎么走") == "map.route.plan"
    assert _intent_tool(offered, "read issue 42") is None


@pytest.mark.django_db
def test_fake_model_does_not_pick_a_tool_for_a_greeting(model_tenant):
    """A greeting must receive prose, not the first offered tool's transcript."""

    _, _, provider, model = model_tenant
    provider.config = {"response": "fake model ready", "tool_call": True}
    provider.save(update_fields=["config"])
    model.capabilities = ["chat", "tools"]
    model.save(update_fields=["capabilities"])
    tools = [
        {
            "type": "function",
            "function": {
                "name": "platform.list_nodes",
                "description": "List cluster nodes",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    result = ModelGateway(guard=_StubGuard()).complete(
        model,
        [{"role": "user", "content": "你好"}],
        tools=tools,
    )

    assert result.tool_calls == []
    assert result.content == "fake model ready"


@override_settings(MODEL_PROVIDER_ALLOWED_HOSTS=["api.example.test"])
def test_gateway_names_the_model_and_provider_reason_on_404(monkeypatch):
    import io
    from urllib.error import HTTPError

    provider = ModelProvider(
        name="Mock OpenAI",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:MODEL_API_KEY",
    )
    model = ModelConfig(
        provider=provider,
        model_name="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        max_output_tokens=200,
    )
    monkeypatch.setenv("MODEL_API_KEY", "test-secret")

    class FakeOpener:
        def open(self, request, timeout):
            raise HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(b'{"error":{"message":"The model does not exist"}}'),
            )

    with patch("core.model_gateway.build_opener", return_value=FakeOpener()):
        with pytest.raises(ModelGatewayError) as raised:
            ModelGateway(guard=_StubGuard()).complete(
                model, [{"role": "user", "content": "hello"}]
            )

    assert raised.value.code == "MODEL_NOT_FOUND"
    assert "gpt-5.6-luna" in str(raised.value)
    assert "The model does not exist" in str(raised.value)


def _openai_models(tenant, user, names):
    providers = []
    models = []
    for name in names:
        provider = ModelProvider.objects.create(
            tenant=tenant,
            created_by=user,
            name=f"{name} provider",
            provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
            base_url=f"https://{name}.example.test/v1",
            credential_ref="env:MODEL_API_KEY",
        )
        model = ModelConfig.objects.create(
            provider=provider,
            model_name=f"{name}-chat",
            display_name=f"{name.title()} Chat",
            capabilities=["chat"],
        )
        providers.append(provider)
        models.append(model)
    return providers, models


@pytest.mark.django_db
def test_retryable_primary_failure_uses_the_configured_fallback(model_tenant, monkeypatch):
    _, tenant, _, _ = model_tenant
    user = tenant.memberships.first().user
    _, (primary, standby) = _openai_models(tenant, user, ["primary", "standby"])
    primary.fallback_model = standby
    primary.save(update_fields=["fallback_model"])
    calls = []

    def fake_post(self, provider, endpoint, body, timeout_seconds, credential_ref=None):
        calls.append(provider.name)
        if provider.name == "primary provider":
            raise ModelGatewayError(
                "MODEL_PROVIDER_UNAVAILABLE",
                "down",
                retryable=True,
                billable_uncertain=True,
            )
        return {
            "choices": [{"message": {"content": "standby answered"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

    monkeypatch.setattr(ModelGateway, "_post_json", fake_post)

    guard = _StubGuard()
    result = ModelGateway(guard=guard).complete(
        primary,
        [{"role": "user", "content": "hello"}],
        idempotency_key="fallback-budget-test",
    )

    assert calls == ["primary provider", "standby provider"]
    assert len(guard.reservations) == 2
    assert guard.reservations[0].idempotency_key != guard.reservations[1].idempotency_key
    assert guard.settlements[0] == (
        "fallback-budget-test",
        guard.reservations[0].reserved_tokens,
    )
    assert result.content == "standby answered"
    assert result.provider == "standby provider"
    assert result.model == "standby-chat"
    assert result.fallback_attempts == [
        {
            "provider": "primary provider",
            "model": "primary-chat",
            "error_code": "MODEL_PROVIDER_UNAVAILABLE",
        }
    ]


@pytest.mark.django_db
def test_non_retryable_failure_does_not_use_the_fallback(model_tenant, monkeypatch):
    _, tenant, _, _ = model_tenant
    user = tenant.memberships.first().user
    _, (primary, standby) = _openai_models(tenant, user, ["primary", "standby"])
    primary.fallback_model = standby
    primary.save(update_fields=["fallback_model"])

    def fake_post(self, provider, endpoint, body, timeout_seconds, credential_ref=None):
        if provider.name == "standby provider":
            pytest.fail("a non-retryable error must not reach the standby")
        raise ModelGatewayError(
            "MODEL_PROVIDER_INVALID_RESPONSE", "broken payload", retryable=False
        )

    monkeypatch.setattr(ModelGateway, "_post_json", fake_post)

    with pytest.raises(ModelGatewayError) as raised:
        ModelGateway(guard=_StubGuard()).complete(
            primary, [{"role": "user", "content": "hello"}]
        )

    assert raised.value.code == "MODEL_PROVIDER_INVALID_RESPONSE"


@pytest.mark.django_db
def test_fallback_is_skipped_when_it_cannot_accept_tools(model_tenant):
    _, tenant, _, _ = model_tenant
    user = tenant.memberships.first().user
    _, (primary, standby) = _openai_models(tenant, user, ["primary", "standby"])
    primary.fallback_model = standby
    primary.save(update_fields=["fallback_model"])

    assert ModelGateway._fallback_model(
        primary,
        [{"type": "function", "function": {"name": "echo", "parameters": {}}}],
    ) is None


@pytest.mark.django_db
def test_api_sets_fallback_and_rejects_a_cycle(model_tenant):
    _, tenant, _, base = model_tenant
    user = tenant.memberships.first().user
    _, (primary, standby) = _openai_models(tenant, user, ["route-a", "route-b"])
    client = APIClient()
    client.force_authenticate(user=user)
    url = f"/api/v1/tenants/{tenant.id}/model-configs/{{}}/"

    set_primary = client.patch(
        url.format(primary.id), {"fallback_model": str(standby.id)}, format="json"
    )
    assert set_primary.status_code == 200
    assert str(set_primary.data["fallback_model"]) == str(standby.id)
    assert set_primary.data["fallback_model_name"] == "Route-B Chat"

    cycle = client.patch(
        url.format(standby.id), {"fallback_model": str(primary.id)}, format="json"
    )
    assert cycle.status_code == 400

    self_reference = client.patch(
        url.format(primary.id), {"fallback_model": str(primary.id)}, format="json"
    )
    assert self_reference.status_code == 400

    cleared = client.patch(url.format(primary.id), {"fallback_model": None}, format="json")
    assert cleared.status_code == 200
    assert cleared.data["fallback_model"] is None
    assert base.fallback_model_id is None
