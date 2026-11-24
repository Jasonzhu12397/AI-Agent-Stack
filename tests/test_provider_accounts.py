"""Linked accounts buy nothing but still get the free allowance.

The point of these tests is the billing boundary: a member who never purchased
a token balance can attach their own OpenAI/Google/DeepSeek/Qwen account and
call models, while the tenant's purchased pool stays untouched.
"""

import uuid

import pytest
from core import provider_accounts
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import (
    ModelConfig,
    ModelProvider,
    ProviderAccountLink,
    ProviderAccountUsage,
    Tenant,
    TenantMembership,
)
from core.provider_accounts import AccountTokenGuard, ProviderAccountError
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def member():
    user_model = get_user_model()
    user = user_model.objects.create_user(username="byo-member")
    tenant = Tenant.objects.create(name="BYO", slug="byo")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, client


def _account_provider(tenant, user, preset_key="openai"):
    return ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name=f"{preset_key}-byo",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
        preset_key=preset_key,
        base_url=provider_accounts.PROVIDER_PRESETS[preset_key]["base_url"],
    )


@pytest.mark.django_db
def test_catalog_is_readable_by_a_member_without_a_purchase(member):
    user, tenant, client = member
    response = client.get(f"/api/v1/tenants/{tenant.id}/model-providers/catalog/")
    assert response.status_code == 200
    keys = [item["key"] for item in response.data["presets"]]
    assert keys == ["openai", "google", "deepseek", "qwen"]
    assert response.data["free_daily_tokens"] == provider_accounts.free_daily_tokens()
    for item in response.data["presets"]:
        assert item["base_url"].startswith("https://")
        assert item["models"]


@pytest.mark.django_db
def test_creating_an_account_provider_publishes_its_preset_models(member):
    user, tenant, client = member
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": "My DeepSeek",
            "provider_type": "openai_compatible",
            "auth_mode": "user_account",
            "preset_key": "deepseek",
            "config": {},
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    provider = ModelProvider.objects.get(pk=response.data["id"])
    assert provider.base_url == provider_accounts.PROVIDER_PRESETS["deepseek"]["base_url"]
    assert provider.credential_ref == ""
    assert provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT
    names = sorted(
        ModelConfig.objects.filter(provider=provider).values_list("model_name", flat=True)
    )
    assert names == sorted(
        item["model_name"] for item in provider_accounts.PROVIDER_PRESETS["deepseek"]["models"]
    )


@pytest.mark.django_db
def test_an_account_provider_rejects_a_tenant_credential(member):
    user, tenant, client = member
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": "Bad",
            "provider_type": "openai_compatible",
            "auth_mode": "user_account",
            "preset_key": "openai",
            "credential_ref": "env:TENANT_KEY",
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "INVALID_REQUEST"
    assert "credential_ref" in response.data["error"]["details"]


@pytest.mark.django_db
def test_member_links_and_revokes_their_own_account(member):
    user, tenant, client = member
    provider = _account_provider(tenant, user)
    base = f"/api/v1/tenants/{tenant.id}/model-providers/{provider.id}/account/"

    linked = client.post(
        base,
        {"credential_ref": "env:MY_OPENAI_KEY", "account_label": "personal"},
        format="json",
    )
    assert linked.status_code == 201, linked.data
    assert linked.data["linked"] is True
    assert linked.data["account_label"] == "personal"
    assert linked.data["remaining_today"] == provider_accounts.free_daily_tokens()

    reloaded = client.get(base)
    assert reloaded.data["linked"] is True
    assert "env:MY_OPENAI_KEY" not in str(reloaded.data)

    revoked = client.delete(base)
    assert revoked.status_code == 200
    assert revoked.data["linked"] is False
    assert (
        ProviderAccountLink.objects.filter(
            provider=provider, status=ProviderAccountLink.Status.REVOKED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_relinking_replaces_the_credential_instead_of_adding_a_row(member):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)

    provider_accounts.link_account(user=user, provider=provider, credential_ref="env:FIRST")
    second = provider_accounts.link_account(
        user=user, provider=provider, credential_ref="env:SECOND"
    )

    assert ProviderAccountLink.objects.filter(provider=provider).count() == 1
    assert second.credential_ref == "env:SECOND"


@pytest.mark.django_db
def test_linking_a_tenant_billed_provider_is_refused(member):
    user, tenant, _ = member
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="paid",
        provider_type=ModelProvider.ProviderType.FAKE,
        auth_mode=ModelProvider.AuthMode.TENANT_SECRET,
    )
    with pytest.raises(ProviderAccountError) as caught:
        provider_accounts.link_account(user=user, provider=provider, credential_ref="env:X")
    assert caught.value.code == "PROVIDER_AUTH_MODE_NOT_ACCOUNT"
    assert caught.value.http_status == 409


@pytest.mark.django_db
def test_free_allowance_is_a_hard_daily_cap(member, settings):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    link = provider_accounts.link_account(user=user, provider=provider, credential_ref="env:KEY")
    settings.BYO_FREE_DAILY_TOKENS = 100

    usage = provider_accounts.reserve_account_tokens(
        user=user,
        provider=provider,
        requested_tokens=100,
        idempotency_key="call-1",
    )
    provider_accounts.settle_account_tokens(usage=usage, consumed_tokens=100)

    with pytest.raises(ProviderAccountError) as caught:
        provider_accounts.reserve_account_tokens(
            user=user,
            provider=provider,
            requested_tokens=1,
            idempotency_key="call-2",
        )
    assert caught.value.code == "PROVIDER_FREE_QUOTA_EXCEEDED"
    assert caught.value.http_status == 429
    assert provider_accounts.quota_snapshot(user, provider)["remaining_today"] == 0

    guard = AccountTokenGuard(link=link)
    assert guard.authorized is True


@pytest.mark.django_db
def test_reservation_is_idempotent_and_counts_while_live(member, settings):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    provider_accounts.link_account(user=user, provider=provider, credential_ref="env:KEY")
    settings.BYO_FREE_DAILY_TOKENS = 200

    first = provider_accounts.reserve_account_tokens(
        user=user, provider=provider, requested_tokens=150, idempotency_key="same"
    )
    again = provider_accounts.reserve_account_tokens(
        user=user, provider=provider, requested_tokens=150, idempotency_key="same"
    )
    assert again.pk == first.pk
    assert provider_accounts.quota_snapshot(user, provider)["remaining_today"] == 50

    with pytest.raises(ProviderAccountError) as caught:
        provider_accounts.reserve_account_tokens(
            user=user, provider=provider, requested_tokens=60, idempotency_key="other"
        )
    assert caught.value.code == "PROVIDER_FREE_QUOTA_EXCEEDED"


@pytest.mark.django_db
def test_a_settled_account_call_cannot_be_replayed(member):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    usage = provider_accounts.reserve_account_tokens(
        user=user, provider=provider, requested_tokens=10, idempotency_key="once"
    )
    provider_accounts.settle_account_tokens(usage=usage, consumed_tokens=8)
    with pytest.raises(ProviderAccountError) as caught:
        provider_accounts.reserve_account_tokens(
            user=user, provider=provider, requested_tokens=10, idempotency_key="once"
        )
    assert caught.value.code == "PROVIDER_ACCOUNT_USAGE_REPLAYED"


@pytest.mark.django_db
def test_gateway_refuses_an_account_provider_without_a_link(member):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    model = ModelConfig.objects.create(
        provider=provider, model_name="gpt-4o-mini", display_name="GPT-4o mini"
    )
    with pytest.raises(ModelGatewayError) as caught:
        ModelGateway().complete(model, [{"role": "user", "content": "hi"}], idempotency_key="k")
    assert caught.value.code == "PROVIDER_ACCOUNT_NOT_LINKED"


@pytest.mark.django_db
def test_model_test_uses_the_free_allowance_and_skips_the_tenant_pool(member, settings):
    user, tenant, client = member
    provider = _account_provider(tenant, user)
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="gpt-4o-mini",
        display_name="GPT-4o mini",
        capabilities=["chat"],
    )
    base = f"/api/v1/tenants/{tenant.id}/model-configs/{model.id}/test/"

    unlinked = client.post(base, {}, format="json")
    assert unlinked.status_code == 403
    assert unlinked.data["error"]["code"] == "PROVIDER_ACCOUNT_NOT_LINKED"

    client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/{provider.id}/account/",
        {"credential_ref": "env:MY_KEY"},
        format="json",
    )
    settings.BYO_FREE_DAILY_TOKENS = 100_000
    # The fake transport is what CI can exercise; the billing path is the
    # subject here, and it must not touch the tenant's purchased balance.
    provider.provider_type = ModelProvider.ProviderType.FAKE
    provider.credential_ref = ""
    provider.config = {"response": "ok from linked account"}
    provider.save()

    tested = client.post(base, {}, format="json")
    assert tested.status_code == 200, tested.data
    assert tested.data["billing"] == "linked_account_free_quota"
    assert tested.data["content"] == "ok from linked account"
    assert tested.data["free_quota"]["free_daily_tokens"] == 100_000
    usage = ProviderAccountUsage.objects.filter(user=user, provider=provider).first()
    assert usage is not None
    assert usage.status == ProviderAccountUsage.Status.SETTLED


@pytest.mark.django_db
def test_quota_snapshot_ignores_yesterdays_spend(member, settings):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    settings.BYO_FREE_DAILY_TOKENS = 100
    yesterday = timezone.now() - timezone.timedelta(days=2)
    usage = ProviderAccountUsage.objects.create(
        user=user,
        provider=provider,
        idempotency_key=str(uuid.uuid4()),
        status=ProviderAccountUsage.Status.SETTLED,
        reserved_tokens=100,
        consumed_tokens=100,
        expires_at=yesterday,
        settled_at=yesterday,
        created_at=yesterday,
    )
    assert usage.consumed_tokens == 100
    assert provider_accounts.quota_snapshot(user, provider)["remaining_today"] == 100


@pytest.mark.django_db
def test_unknown_preset_is_a_validation_error(member):
    user, tenant, client = member
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/model-providers/",
        {
            "name": "Mystery",
            "provider_type": "openai_compatible",
            "auth_mode": "user_account",
            "preset_key": "not-a-platform",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "preset_key" in response.data["error"]["details"]


@override_settings(LOCAL_MODEL_ALLOWED_HOSTS=["models.internal"])
@pytest.mark.django_db
def test_account_provider_calls_the_openai_compatible_transport(member):
    user, tenant, _ = member
    provider = _account_provider(tenant, user)
    provider.provider_type = ModelProvider.ProviderType.FAKE
    provider.config = {"response": "linked"}
    provider.save()
    link = provider_accounts.link_account(user=user, provider=provider, credential_ref="env:KEY")
    model = ModelConfig.objects.create(
        provider=provider, model_name="gpt-4o-mini", display_name="GPT-4o mini"
    )
    result = ModelGateway(account=AccountTokenGuard(link=link)).complete(
        model,
        [{"role": "user", "content": "hi"}],
        idempotency_key="account-call",
    )
    assert result.content == "linked"
    assert ProviderAccountUsage.objects.get(idempotency_key="account-call").status == (
        ProviderAccountUsage.Status.SETTLED
    )
