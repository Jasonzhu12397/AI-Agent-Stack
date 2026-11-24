"""Authorization without a copied key: OpenRouter issues the key for us."""

import pytest
from core import provider_accounts, provider_oauth
from core.field_crypto import open_sealed
from core.models import ModelProvider, Tenant, TenantMembership
from core.provider_oauth import OAuthFlowError, complete_flow, start_flow
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

User = get_user_model()
REDIRECT = "https://console.example.dev/marketplace?oauth=openrouter"


@pytest.fixture
def workspace(db):
    opener = User.objects.create_user(username="oauth-seed", password="pw-oauth-1234")
    tenant = Tenant.objects.create(name="OAuth tenant", slug="oauth-tenant")
    member = User.objects.create_user(username="oauth-member", password="pw-oauth-1234")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    return opener, tenant, member


def test_start_url_carries_only_the_challenge(workspace):
    _opener, tenant, member = workspace

    attempt = start_flow(
        user=member, tenant=tenant, preset_key="openrouter", redirect_uri=REDIRECT
    )

    assert attempt.authorization_url.startswith("https://openrouter.ai/auth?")
    assert "code_challenge_method=S256" in attempt.authorization_url
    assert "code_challenge=" in attempt.authorization_url
    row = provider_accounts.ProviderOAuthState.objects.get(state=attempt.state)
    verifier = open_sealed(row.sealed_verifier, "provider-oauth-verifier")
    assert row.user == member
    assert "code_challenge=" + provider_oauth.code_challenge_for(verifier) in (
        attempt.authorization_url
    )
    # The verifier itself must never appear in the URL the browser visits.
    assert verifier not in attempt.authorization_url


@pytest.mark.django_db
def test_start_rejects_a_preset_without_an_authorization_flow(workspace):
    _opener, tenant, member = workspace

    with pytest.raises(OAuthFlowError) as caught:
        start_flow(user=member, tenant=tenant, preset_key="openai", redirect_uri=REDIRECT)

    assert caught.value.code == "OAUTH_PRESET_UNSUPPORTED"


@pytest.mark.django_db
def test_completed_flow_creates_a_linked_provider(workspace, monkeypatch):
    _opener, tenant, member = workspace
    attempt = start_flow(
        user=member, tenant=tenant, preset_key="openrouter", redirect_uri=REDIRECT
    )
    seen = {}

    def _fake_key_request(url, payload, timeout=20):
        seen.update(url=url, payload=payload)
        return {"key": "sk-or-issued-by-platform", "label": "jason@example.com"}

    monkeypatch.setattr(provider_oauth, "post_key_request", _fake_key_request)

    provider, link = complete_flow(
        user=member, tenant=tenant, preset_key="openrouter", code="auth-code", state=attempt.state
    )

    assert seen["url"] == "https://openrouter.ai/api/v1/auth/keys"
    assert seen["payload"]["code"] == "auth-code"
    assert "code_verifier" in seen["payload"]
    assert provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT
    assert provider.models.count() == 3
    assert link.credential_ref.startswith("member://")
    assert provider_accounts.resolve_member_secret(link.credential_ref, member) == (
        "sk-or-issued-by-platform"
    )
    assert link.account_label == "jason@example.com"


@pytest.mark.django_db
def test_a_code_cannot_be_redeemed_twice(workspace, monkeypatch):
    _opener, tenant, member = workspace
    attempt = start_flow(
        user=member, tenant=tenant, preset_key="openrouter", redirect_uri=REDIRECT
    )
    monkeypatch.setattr(
        provider_oauth, "post_key_request", lambda *a, **k: {"key": "sk-or-once"}
    )
    complete_flow(
        user=member, tenant=tenant, preset_key="openrouter", code="c1", state=attempt.state
    )

    with pytest.raises(OAuthFlowError) as caught:
        complete_flow(
            user=member, tenant=tenant, preset_key="openrouter", code="c1", state=attempt.state
        )

    assert caught.value.code == "OAUTH_STATE_UNKNOWN"


@pytest.mark.django_db
def test_expired_attempt_is_refused(workspace, monkeypatch):
    _opener, tenant, member = workspace
    attempt = start_flow(
        user=member, tenant=tenant, preset_key="openrouter", redirect_uri=REDIRECT
    )
    row = provider_accounts.ProviderOAuthState.objects.get(state=attempt.state)
    row.expires_at = timezone.now() - timezone.timedelta(minutes=1)
    row.save(update_fields=["expires_at"])
    monkeypatch.setattr(provider_oauth, "post_key_request", lambda *a, **k: {"key": "sk-or"})

    with pytest.raises(OAuthFlowError) as caught:
        complete_flow(
            user=member, tenant=tenant, preset_key="openrouter", code="c", state=attempt.state
        )

    assert caught.value.code == "OAUTH_STATE_EXPIRED"


@pytest.mark.django_db
def test_platform_refusal_surfaces_as_a_failed_exchange(workspace, monkeypatch):
    _opener, tenant, member = workspace
    attempt = start_flow(
        user=member, tenant=tenant, preset_key="openrouter", redirect_uri=REDIRECT
    )

    def _refuse(*_args, **_kwargs):
        raise OAuthFlowError("OAUTH_EXCHANGE_REFUSED", "refused", http_status=400)

    monkeypatch.setattr(provider_oauth, "post_key_request", _refuse)

    with pytest.raises(OAuthFlowError) as caught:
        complete_flow(
            user=member, tenant=tenant, preset_key="openrouter", code="c", state=attempt.state
        )

    assert caught.value.code == "OAUTH_EXCHANGE_REFUSED"


@pytest.mark.django_db
def test_api_start_and_exchange(workspace, monkeypatch):
    _opener, tenant, member = workspace
    client = APIClient()
    client.force_login(member)

    started = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/oauth/openrouter/start/",
        {"redirect_uri": REDIRECT},
        format="json",
    )
    assert started.status_code == 201
    body = started.json()
    assert body["authorization_url"].startswith("https://openrouter.ai/auth?")

    monkeypatch.setattr(
        provider_oauth, "post_key_request", lambda *a, **k: {"key": "sk-or-from-api"}
    )
    exchanged = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/oauth/openrouter/exchange/",
        {"code": "auth-code", "state": body["state"]},
        format="json",
    )

    assert exchanged.status_code == 201
    payload = exchanged.json()
    assert payload["account"]["linked"] is True
    assert payload["provider"]["preset_key"] == "openrouter"
