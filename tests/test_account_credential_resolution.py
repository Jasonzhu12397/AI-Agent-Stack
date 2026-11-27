"""A member who pastes a platform key can call that platform's models at once."""

import pytest
from core import provider_accounts
from core.model_gateway import ModelGateway, ModelGatewayError, resolve_credential
from core.models import (
    ModelConfig,
    ModelProvider,
    Project,
    ProjectMembership,
    ProviderAccountLink,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def workspace(db):
    opener = User.objects.create_user(username="cred-seed", password="pw-cred-1234")
    tenant = Tenant.objects.create(name="Credential tenant", slug="credential-tenant")
    project = Project.objects.create(tenant=tenant, name="Cred project", slug="cred-project")
    with transaction.atomic():
        provider = ModelProvider.objects.create(
            tenant=tenant,
            created_by=opener,
            name="OpenAI (my account)",
            provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
            auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
            preset_key="openai",
            base_url="https://api.openai.com/v1",
        )
    return tenant, project, provider


def _member(username):
    user = User.objects.create_user(username=username, password="pw-cred-1234")
    return user


@pytest.mark.django_db
def test_pasted_key_is_sealed_and_resolvable(workspace):
    tenant, _project, provider = workspace
    member = _member("member")

    link = provider_accounts.link_account(
        user=member, provider=provider, credential_ref="sk-live-abc123", account_label="me@x.com"
    )

    assert link.credential_ref.startswith("member://")
    secret = provider_accounts.MemberCredentialSecret.objects.get()
    assert "sk-live-abc123" not in secret.sealed_value
    assert resolve_credential(link.credential_ref, member) == "sk-live-abc123"


@pytest.mark.django_db
def test_other_member_cannot_read_the_sealed_key(workspace):
    _tenant, _project, provider = workspace
    owner = _member("owner")
    stranger = _member("stranger")

    link = provider_accounts.link_account(
        user=owner, provider=provider, credential_ref="sk-owner-only"
    )

    assert provider_accounts.resolve_member_secret(link.credential_ref, stranger) is None
    with pytest.raises(ModelGatewayError) as caught:
        resolve_credential(link.credential_ref, stranger)
    assert caught.value.code == "MODEL_CREDENTIAL_MISSING"


@pytest.mark.django_db
def test_explicit_env_reference_is_left_untouched(workspace):
    _tenant, _project, provider = workspace
    member = _member("envuser")

    link = provider_accounts.link_account(
        user=member, provider=provider, credential_ref="env:OPENAI_API_KEY"
    )

    assert link.credential_ref == "env:OPENAI_API_KEY"
    assert provider_accounts.MemberCredentialSecret.objects.count() == 0


@pytest.mark.django_db
def test_relinking_rotates_the_sealed_key(workspace):
    _tenant, _project, provider = workspace
    member = _member("rotate")

    first = provider_accounts.link_account(
        user=member, provider=provider, credential_ref="sk-first"
    )
    second = provider_accounts.link_account(
        user=member, provider=provider, credential_ref="sk-second"
    )

    assert ProviderAccountLink.objects.filter(user=member, provider=provider).count() == 1
    assert second.credential_ref != first.credential_ref
    assert resolve_credential(second.credential_ref, member) == "sk-second"


@pytest.mark.django_db
def test_gateway_reads_the_account_owner_key(workspace):
    _tenant, _project, provider = workspace
    member = _member("caller")

    link = provider_accounts.link_account(
        user=member, provider=provider, credential_ref="sk-call-me"
    )
    guard = provider_accounts.AccountTokenGuard(link=link)
    gateway = ModelGateway(guard=guard, account=guard)

    assert gateway._account_user == member
    assert resolve_credential(guard.credential_ref, gateway._account_user) == "sk-call-me"


@pytest.mark.django_db
def test_authorized_account_model_appears_in_chat_context(workspace):
    tenant, project, provider = workspace
    member = _member("chatter")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    ProjectMembership.objects.create(
        project=project, user=member, role=ProjectMembership.Role.VIEWER
    )
    # A personal platform row belongs to the member who created it; the chat
    # context intentionally hides rows owned by somebody else.
    provider.created_by = member
    provider.save(update_fields=["created_by"])
    provider_accounts.link_account(user=member, provider=provider, credential_ref="sk-chat")
    ModelConfig.objects.create(
        provider=provider,
        model_name="gpt-4o-mini",
        version=1,
        display_name="GPT-4o mini",
        capabilities=["chat"],
    )
    client = APIClient()
    client.force_login(member)

    response = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"
    )

    assert response.status_code == 200
    row = next(
        item for item in response.json()["models"] if item["provider_name"] == provider.name
    )
    assert row["account"]["linked"] is True
    assert row["account"]["remaining_today"] > 0


def _config_for(provider):
    model = provider.models.first() or ModelConfig.objects.create(
        provider=provider,
        model_name="gpt-4o-mini",
        version=1,
        display_name="GPT-4o mini",
        capabilities=["chat"],
    )
    return model


@pytest.mark.django_db
def test_rejected_key_reports_the_credential_not_an_outage(workspace, monkeypatch):
    """A 401 from the platform means "paste a new key", not "provider is down"."""

    _tenant, _project, provider = workspace
    member = _member("badkey")
    provider_accounts.link_account(user=member, provider=provider, credential_ref="sk-revoked")
    guard = provider_accounts.AccountTokenGuard(
        link=provider_accounts.active_link(member, provider)
    )
    gateway = ModelGateway(guard=guard, account=guard)

    def _raise(*_args, **_kwargs):
        from urllib.error import HTTPError

        raise HTTPError("https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {}, None)

    import core.model_gateway as gateway_module

    monkeypatch.setattr(
        gateway_module, "build_opener", lambda *_a, **_k: type("O", (), {"open": _raise})()
    )

    with pytest.raises(ModelGatewayError) as caught:
        gateway.complete(
            idempotency_key="test-credential-rejected",
            model_config=_config_for(provider),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            timeout_seconds=5,
        )

    assert caught.value.code == "MODEL_CREDENTIAL_REJECTED"
    assert caught.value.retryable is False


def test_both_environment_spellings_resolve(monkeypatch):
    """``env:NAME`` and ``env://NAME`` are the same reference, so both work."""

    monkeypatch.setenv("DEMO_MODEL_KEY", "sk-env-value")

    assert resolve_credential("env:DEMO_MODEL_KEY") == "sk-env-value"
    assert resolve_credential("env://DEMO_MODEL_KEY") == "sk-env-value"


def test_missing_environment_names_the_variable(monkeypatch):
    monkeypatch.delenv("PLUGIN_SECRET_MODEL_KEY", raising=False)

    with pytest.raises(ModelGatewayError) as caught:
        resolve_credential("env://PLUGIN_SECRET_MODEL_KEY")

    assert caught.value.code == "MODEL_CREDENTIAL_MISSING"
    assert "PLUGIN_SECRET_MODEL_KEY" in str(caught.value)
