"""Enforcement tests for the mandatory token-budget guard on every provider call.

The gateway is the single metering entry point: a call without an authorized
guard must fail before any network request is issued, and every metered call
leaves a row in the tenant token usage ledger.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import (
    AuditEvent,
    ModelConfig,
    ModelProvider,
    Project,
    Task,
    Tenant,
    TenantMembership,
    TenantTokenBudget,
    TenantTokenUsage,
)
from core.token_budget import TenantTokenBudgetExceeded
from core.token_guard import AdHocTokenGuard
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient
from django.utils import timezone


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = [REPO_ROOT / "apps"]


@pytest.fixture
def guarded_tenant(db):
    user = get_user_model().objects.create_user(
        username="guard-tenant-admin",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Guard tenant", slug="guard-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Guard project", slug="guard-project"
    )
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Guard fake",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "fake guard answer"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="guard-fake-chat",
        display_name="Guard Fake Chat",
        capabilities=["chat"],
    )
    TenantTokenBudget.objects.create(tenant=tenant, daily_limit=100_000, monthly_limit=1_000_000)
    return user, tenant, project, model


class _UnauthorizedGuard:
    authorized = False

    def reserve(self, **kwargs):
        raise AssertionError("reserve must not be called on an unauthorized guard")

    def settle(self, **kwargs):
        raise AssertionError("settle must not be called on an unauthorized guard")

    def release(self, **kwargs):
        raise AssertionError("release must not be called on an unauthorized guard")


def test_gateway_requires_guard(guarded_tenant):
    _, _, _, model = guarded_tenant

    with patch("core.model_gateway.build_opener") as opener:
        with pytest.raises(ModelGatewayError) as complete_error:
            ModelGateway().complete(model, [{"role": "user", "content": "hello"}])
    assert complete_error.value.code == "TOKEN_GUARD_REQUIRED"
    assert complete_error.value.retryable is False
    opener.assert_not_called()

    embedding_provider = ModelProvider(
        name="Embed guard provider",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="https://api.example.test/v1",
        credential_ref="env:MODEL_API_KEY",
    )
    embedding_model = ModelConfig(
        provider=embedding_provider,
        model_name="guard-embed",
        display_name="Guard Embed",
        capabilities=["embeddings"],
    )
    with patch("core.model_gateway.build_opener") as opener:
        with pytest.raises(ModelGatewayError) as embed_error:
            ModelGateway().embed(embedding_model, ["hello"])
    assert embed_error.value.code == "TOKEN_GUARD_REQUIRED"
    assert embed_error.value.retryable is False
    opener.assert_not_called()


def test_bypassing_reservation_fails_before_network(guarded_tenant):
    _, _, _, model = guarded_tenant

    gateway = ModelGateway(guard=_UnauthorizedGuard())
    with patch("core.model_gateway.build_opener") as opener:
        with pytest.raises(ModelGatewayError) as error:
            gateway.complete(model, [{"role": "user", "content": "hello"}])
    assert error.value.code == "TOKEN_GUARD_REQUIRED"
    opener.assert_not_called()


@pytest.mark.django_db
def test_model_test_view_reserves_and_429_on_exhaustion(guarded_tenant):
    user, tenant, _, model = guarded_tenant
    client = APIClient()
    client.force_authenticate(user=user)
    url = f"/api/v1/tenants/{tenant.id}/model-configs/{model.id}/test/"

    response = client.post(url, {"messages": [{"role": "user", "content": "hello"}]}, format="json")
    assert response.status_code == 200

    ledger = TenantTokenUsage.objects.filter(tenant=tenant).order_by("created_at")
    assert ledger.count() == 1
    row = ledger.first()
    assert row.scope_ref.startswith("model-test:")
    assert row.agent_run_id is None
    assert row.workflow_run_id is None
    assert row.status == TenantTokenUsage.Status.SETTLED

    budget = TenantTokenBudget.objects.get(tenant=tenant)
    budget.daily_limit = 0
    budget.monthly_limit = 0
    budget.save(update_fields=["daily_limit", "monthly_limit", "updated_at"])

    exhausted = client.post(
        url, {"messages": [{"role": "user", "content": "again"}]}, format="json"
    )
    assert exhausted.status_code == 429
    body = exhausted.data["error"]
    assert body["code"] == "TENANT_TOKEN_BUDGET_EXCEEDED"
    assert body["details"]["period"] in {"daily", "monthly"}

    failure_audit = AuditEvent.objects.filter(
        tenant=tenant,
        event_type="model.config.tested",
        outcome=AuditEvent.Outcome.FAILURE,
    ).exists()
    assert failure_audit


def _embedding_provider(tenant, user):
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Guard embed",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        base_url="http://ollama:11434/v1",
        credential_ref="",
    )
    return ModelConfig.objects.create(
        provider=provider,
        model_name="guard-embed",
        display_name="Guard Embed",
        capabilities=["embeddings"],
    )


class _FakeEmbeddingResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        import json

        data = json.dumps(self.payload).encode("utf-8")
        return data if size < 0 else data[:size]


class _FakeEmbeddingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return _FakeEmbeddingResponse(self.payload)


@pytest.mark.django_db
@override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["ollama"])
def test_embedding_reserves_tokens(guarded_tenant):
    user, tenant, _, _ = guarded_tenant
    model = _embedding_provider(tenant, user)
    guard = AdHocTokenGuard(tenant=tenant, scope_ref=f"rag-test:{model.id}")
    opener = _FakeEmbeddingOpener(
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
    )

    with patch("core.model_gateway.build_opener", return_value=opener):
        response = ModelGateway(guard=guard).embed(
            model, ["hello embedding"], idempotency_key="rag-test-embed-1"
        )

    assert response.vectors == [[0.1, 0.2]]
    row = TenantTokenUsage.objects.get(tenant=tenant, scope_ref=f"rag-test:{model.id}")
    assert row.status == TenantTokenUsage.Status.SETTLED
    assert row.reserved_tokens == 0
    assert row.consumed_tokens > 0


def test_no_unmetered_gateway_construction_in_source():
    import re

    construction = re.compile(r"ModelGateway\s*\(")
    bare_call = re.compile(r"ModelGateway\s*\(\s*\)\s*\.(?:complete|embed)\s*\(")
    offenders = []

    for source_dir in SOURCE_DIRS:
        for path in source_dir.rglob("*.py"):
            if "__pycache__" in path.parts or "vendor" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in construction.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start:line_end]
                if "guard=" not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
            for match in bare_call.finditer(text):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: bare call {match.group(0)}"
                )

    assert not offenders, "Unguarded ModelGateway construction/call found:\n" + "\n".join(offenders)
