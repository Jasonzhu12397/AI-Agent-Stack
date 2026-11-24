"""Bring-your-own-account model providers and their free allowance.

A tenant that has not bought any token balance can still let its members use
OpenAI, Google, DeepSeek or Qwen through their own account on those platforms.
The provider record is only a catalogue entry in that case: the credential
belongs to the linked member, the call is billed to nobody, and it draws on a
free per-member daily allowance instead of the tenant's purchased pool.

The catalogue and the allowance are data: presets are declarations, the limit
comes from settings, and the ledger decides what is still available. Nothing
here hard-codes a vendor URL into the request path.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from core.models import ModelProvider, ProviderAccountLink, ProviderAccountUsage

#: Model platforms a member may attach their own account to. ``base_url`` is
#: the OpenAI-compatible endpoint each platform exposes, so one transport
#: serves all of them and a new platform is a line in this table.
PROVIDER_PRESETS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"model_name": "gpt-4o-mini", "display_name": "GPT-4o mini"},
            {"model_name": "gpt-4o", "display_name": "GPT-4o"},
        ],
    },
    "google": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": [
            {"model_name": "gemini-2.0-flash", "display_name": "Gemini 2.0 Flash"},
            {"model_name": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro"},
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"model_name": "deepseek-chat", "display_name": "DeepSeek Chat"},
            {"model_name": "deepseek-reasoner", "display_name": "DeepSeek Reasoner"},
        ],
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"model_name": "glm-4-plus", "display_name": "GLM-4 Plus"},
            {"model_name": "glm-4-flash", "display_name": "GLM-4 Flash"},
        ],
    },
    "moonshot": {
        "label": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            {"model_name": "moonshot-v1-8k", "display_name": "Kimi 8K"},
            {"model_name": "moonshot-v1-32k", "display_name": "Kimi 32K"},
        ],
    },
    "minimax": {
        "label": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "models": [
            {"model_name": "abab6.5s-chat", "display_name": "MiniMax abab6.5s"},
        ],
    },
    "baichuan": {
        "label": "百川",
        "base_url": "https://api.baichuan-ai.com/v1",
        "models": [
            {"model_name": "Baichuan4", "display_name": "Baichuan4"},
        ],
    },
    "stepfun": {
        "label": "阶跃星辰",
        "base_url": "https://api.stepfun.com/v1",
        "models": [
            {"model_name": "step-1-8k", "display_name": "Step-1 8K"},
        ],
    },
    "yi": {
        "label": "零一万物",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "models": [
            {"model_name": "yi-lightning", "display_name": "Yi Lightning"},
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {"model_name": "openai/gpt-4o-mini", "display_name": "GPT-4o mini (via OpenRouter)"},
            {"model_name": "openai/gpt-4o", "display_name": "GPT-4o (via OpenRouter)"},
            {
                "model_name": "deepseek/deepseek-chat",
                "display_name": "DeepSeek Chat (via OpenRouter)",
            },
        ],
        #: OpenRouter is the platform that can authorize an account and issue
        #: the key itself, so the member never copies one.
        "oauth": {
            "authorize_url": "https://openrouter.ai/auth",
            "key_endpoint": "https://openrouter.ai/api/v1/auth/keys",
            "key_label": "AI Agent Studio",
        },
    },
    "qwen": {
        "label": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"model_name": "qwen-plus", "display_name": "Qwen Plus"},
            {"model_name": "qwen-max", "display_name": "Qwen Max"},
        ],
    },
}

#: Tokens a member may spend per local day through linked accounts. Overridable
#: per deployment because the number is a product decision, not a protocol one.
DEFAULT_FREE_DAILY_TOKENS = 50_000
RESERVATION_TTL_SECONDS = 300
MAX_ACCOUNT_LABEL_LENGTH = 120


class ProviderOAuthState(models.Model):
    """One in-flight authorization attempt for a member and a platform."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="provider_oauth_states",
    )
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE, related_name="+")
    preset_key = models.CharField(max_length=64)
    state = models.CharField(max_length=128, unique=True)
    sealed_verifier = models.TextField()
    redirect_uri = models.TextField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"oauth-state:{self.preset_key}:{self.user_id}"


class ProviderAccountError(Exception):
    """A link or free-allowance rule was violated."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def free_daily_tokens() -> int:
    raw = os.getenv("BYO_FREE_DAILY_TOKENS", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    configured = getattr(settings, "BYO_FREE_DAILY_TOKENS", None)
    if isinstance(configured, int) and not isinstance(configured, bool):
        return max(0, configured)
    return DEFAULT_FREE_DAILY_TOKENS


def preset_for(key: str) -> dict:
    preset = PROVIDER_PRESETS.get(key)
    if preset is None:
        raise ProviderAccountError(
            "PROVIDER_PRESET_UNKNOWN",
            f"Unknown model platform preset: {key}.",
        )
    return preset


def catalogue() -> list[dict]:
    """Every attachable platform with the models it publishes."""

    return [
        {
            "key": key,
            "label": preset["label"],
            "base_url": preset["base_url"],
            "models": [dict(item) for item in preset["models"]],
            #: True when the platform can grant access itself, so the console
            #: offers "Authorize" instead of asking for a pasted key.
            "oauth": bool(preset.get("oauth")),
        }
        for key, preset in PROVIDER_PRESETS.items()
    ]


def is_account_provider(provider) -> bool:
    return provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT


def active_link(user, provider) -> ProviderAccountLink | None:
    return (
        ProviderAccountLink.objects.filter(
            user=user,
            provider=provider,
            status=ProviderAccountLink.Status.LINKED,
        )
        .order_by("-linked_at")
        .first()
    )


@transaction.atomic
def link_account(
    *, user, provider, credential_ref: str, account_label: str = ""
) -> ProviderAccountLink:
    """Record that ``user`` attached their own account on ``provider``.

    Re-linking replaces the credential reference of the active link instead of
    creating a second row, so a member who rotates their own key does not
    accumulate links the quota would have to deduplicate later.
    """

    if not is_account_provider(provider):
        raise ProviderAccountError(
            "PROVIDER_AUTH_MODE_NOT_ACCOUNT",
            "This provider is billed to the tenant; account linking does not apply.",
            http_status=409,
        )
    reference = _storable_reference(credential_ref, user)
    if not reference:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_CREDENTIAL_REQUIRED",
            "A credential reference for your own account is required.",
        )
    label = (account_label or "").strip()[:MAX_ACCOUNT_LABEL_LENGTH]
    existing = ProviderAccountLink.objects.filter(
        user=user,
        provider=provider,
        status=ProviderAccountLink.Status.LINKED,
    ).first()
    if existing is not None:
        existing.credential_ref = reference
        existing.account_label = label
        existing.linked_at = timezone.now()
        existing.save(update_fields=["credential_ref", "account_label", "linked_at", "updated_at"])
        return existing
    return ProviderAccountLink.objects.create(
        user=user,
        provider=provider,
        credential_ref=reference,
        account_label=label,
    )


def seed_preset_models(provider) -> int:
    """Publish the preset's models so a linked member has something to call."""

    from core.models import ModelConfig

    preset = PROVIDER_PRESETS.get(provider.preset_key) or {"models": []}
    created = 0
    for item in preset["models"]:
        _, made = ModelConfig.objects.get_or_create(
            provider=provider,
            model_name=item["model_name"],
            version=1,
            defaults={
                "display_name": item["display_name"],
                "capabilities": ["chat", "tools"],
                "input_cost_per_million": 0,
                "output_cost_per_million": 0,
            },
        )
        created += int(made)
    return created


def ensure_account_provider(*, tenant, user, preset_key: str) -> ModelProvider:
    """The member's own catalogue row for a platform, created once.

    Authorization returns to a tenant where the row may not exist yet, so this
    is the single place that decides the name, the preset and the seeded models.
    """

    preset = PROVIDER_PRESETS.get(preset_key)
    if preset is None:
        raise ProviderAccountError(
            "PROVIDER_PRESET_UNKNOWN", f"No platform preset named '{preset_key}'."
        )
    existing = ModelProvider.objects.filter(
        tenant=tenant,
        created_by=user,
        preset_key=preset_key,
        auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
    ).first()
    if existing is not None:
        return existing
    name = f"{preset['label']} (my account)"
    candidate, suffix = name, 2
    with transaction.atomic():
        while ModelProvider.objects.filter(tenant=tenant, name=candidate).exists():
            candidate = f"{name} {suffix}"
            suffix += 1
        provider = ModelProvider.objects.create(
            tenant=tenant,
            created_by=user,
            name=candidate,
            provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
            auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
            preset_key=preset_key,
            base_url=preset["base_url"],
        )
        seed_preset_models(provider)
    return provider


_MEMBER_REF_PREFIX = "member://"
_MEMBER_SCOPE = "member-credential-secret"


def _storable_reference(value: str | None, user) -> str:
    """Turn whatever the member pasted into a reference that can be resolved.

    A key pasted straight from the platform has no scheme, so it is sealed into
    a member secret and stored as ``member://<id>``. An explicit ``env://`` or
    ``vault://`` reference is kept as-is, because an operator who injects
    credentials at boot stays authoritative.
    """

    reference = (value or "").strip()
    if not reference:
        return ""
    if "://" in reference or reference.startswith("env:"):
        return reference
    from core.field_crypto import seal

    secret = MemberCredentialSecret.objects.create(
        user=user, sealed_value=seal(reference, _MEMBER_SCOPE)
    )
    return f"{_MEMBER_REF_PREFIX}{secret.id}"


def resolve_member_secret(reference: str, user=None) -> str | None:
    """Plaintext behind a ``member://`` reference, scoped to its owner."""

    if not reference.startswith(_MEMBER_REF_PREFIX):
        return None
    secret_id = reference[len(_MEMBER_REF_PREFIX) :]
    queryset = MemberCredentialSecret.objects.filter(pk=secret_id)
    if user is not None:
        queryset = queryset.filter(user=user)
    secret = queryset.first()
    if secret is None:
        return None
    from core.field_crypto import open_sealed

    return open_sealed(secret.sealed_value, _MEMBER_SCOPE)


@transaction.atomic
def unlink_account(*, user, provider) -> ProviderAccountLink:
    link = active_link(user, provider)
    if link is None:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_NOT_LINKED",
            "No account of yours is linked to this provider.",
            http_status=404,
        )
    link.status = ProviderAccountLink.Status.REVOKED
    link.revoked_at = timezone.now()
    link.credential_ref = ""
    link.save(update_fields=["status", "revoked_at", "credential_ref", "updated_at"])
    return link


def account_usage(user, provider):
    """Rows that count against today's free allowance."""

    now = timezone.now()
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return ProviderAccountUsage.objects.filter(
        user=user,
        provider=provider,
        created_at__gte=day_start,
    )


def consumed_today(user, provider) -> int:
    """Settled tokens plus live reservations, so the allowance is fail-closed."""

    now = timezone.now()
    total = 0
    for row in account_usage(user, provider):
        if row.status == ProviderAccountUsage.Status.SETTLED:
            total += row.consumed_tokens
        elif row.status == ProviderAccountUsage.Status.RESERVED and row.expires_at > now:
            total += row.reserved_tokens
    return total


def quota_snapshot(user, provider) -> dict:
    limit = free_daily_tokens()
    used = consumed_today(user, provider)
    return {
        "free_daily_tokens": limit,
        "used_today": used,
        "remaining_today": max(0, limit - used),
        "exhausted": used >= limit,
    }


def _assert_usage_key(idempotency_key: str) -> None:
    if not idempotency_key or len(idempotency_key) > 160:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_USAGE_KEY_INVALID",
            "An idempotency key of at most 160 characters is required.",
        )


@transaction.atomic
def reserve_account_tokens(*, user, provider, requested_tokens: int, idempotency_key: str):
    """Reserve part of today's free allowance for one call.

    Reserved rows count against the allowance while they are live, so two
    concurrent calls cannot both see the full remainder. A reservation that is
    replayed with the same key returns the original row instead of consuming
    the allowance twice.
    """

    if isinstance(requested_tokens, bool) or not isinstance(requested_tokens, int):
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_TOKENS_INVALID",
            "requested_tokens must be a positive integer.",
        )
    if requested_tokens < 1:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_TOKENS_INVALID",
            "requested_tokens must be a positive integer.",
        )
    _assert_usage_key(idempotency_key)
    now = timezone.now()
    usage, created = ProviderAccountUsage.objects.select_for_update().get_or_create(
        user=user,
        provider=provider,
        idempotency_key=idempotency_key,
        defaults={
            "status": ProviderAccountUsage.Status.RELEASED,
            "reserved_tokens": 0,
            "expires_at": now,
        },
    )
    if not created:
        if usage.status == ProviderAccountUsage.Status.SETTLED:
            raise ProviderAccountError(
                "PROVIDER_ACCOUNT_USAGE_REPLAYED",
                "A settled account call cannot be replayed with the same idempotency key.",
                http_status=409,
            )
        if usage.status == ProviderAccountUsage.Status.RESERVED and usage.expires_at > now:
            if usage.reserved_tokens != requested_tokens:
                raise ProviderAccountError(
                    "PROVIDER_ACCOUNT_USAGE_CHANGED",
                    "The reserved token amount changed for an existing call.",
                    http_status=409,
                )
            return usage

    remaining = quota_snapshot(user, provider)["remaining_today"]
    if requested_tokens > remaining:
        raise ProviderAccountError(
            "PROVIDER_FREE_QUOTA_EXCEEDED",
            "Today's free allowance for this linked account is exhausted.",
            http_status=429,
        )
    usage.status = ProviderAccountUsage.Status.RESERVED
    usage.reserved_tokens = requested_tokens
    usage.consumed_tokens = 0
    usage.expires_at = now + timedelta(seconds=RESERVATION_TTL_SECONDS)
    usage.created_at = now
    usage.save()
    return usage


@transaction.atomic
def settle_account_tokens(*, usage, consumed_tokens: int):
    if usage.status == ProviderAccountUsage.Status.SETTLED:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_USAGE_SETTLED",
            "This account reservation is already settled.",
            http_status=409,
        )
    if (
        isinstance(consumed_tokens, bool)
        or not isinstance(consumed_tokens, int)
        or consumed_tokens < 0
    ):
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_TOKENS_INVALID",
            "consumed_tokens must be a non-negative integer.",
        )
    usage.status = ProviderAccountUsage.Status.SETTLED
    usage.consumed_tokens = consumed_tokens
    usage.settled_at = timezone.now()
    usage.save()
    return usage


@transaction.atomic
def release_account_tokens(*, usage):
    if usage.status == ProviderAccountUsage.Status.SETTLED:
        return usage
    usage.status = ProviderAccountUsage.Status.RELEASED
    usage.reserved_tokens = 0
    usage.save(update_fields=["status", "reserved_tokens", "updated_at"])
    return usage


def describe_account(link: ProviderAccountLink | None) -> dict:
    if link is None:
        return {"linked": False}
    return {
        "linked": True,
        "account_label": link.account_label,
        "linked_at": link.linked_at.isoformat() if link.linked_at else None,
    }


def new_usage_key(prefix: str = "account-call") -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


class AccountTokenGuard:
    """Guard-shaped adapter over the free allowance of a linked account.

    ``ModelGateway`` only needs reserve/release/settle plus the credential it
    should resolve. Keeping the same shape means the billing difference
    between a purchased pool and a free allowance lives in the guard, not in
    branches scattered through the request path.
    """

    def __init__(self, *, link):
        self.link = link

    @property
    def authorized(self) -> bool:
        return self.link is not None and self.link.status == ProviderAccountLink.Status.LINKED

    @property
    def credential_ref(self) -> str:
        return self.link.credential_ref if self.link is not None else ""

    def reserve(
        self, *, idempotency_key, requested_tokens, call_type="complete", model_config=None
    ):
        if not self.authorized:
            raise ProviderAccountError(
                "PROVIDER_ACCOUNT_NOT_LINKED",
                "Link your own account on this platform before calling its models.",
                http_status=403,
            )
        return reserve_account_tokens(
            user=self.link.user,
            provider=self.link.provider,
            requested_tokens=requested_tokens,
            idempotency_key=idempotency_key,
        )

    def release(self, *, reservation):
        return release_account_tokens(usage=reservation)

    def settle(self, *, reservation, consumed_tokens):
        return settle_account_tokens(usage=reservation, consumed_tokens=consumed_tokens)


class MemberCredentialSecret(models.Model):
    """A platform credential a member typed into the console, sealed at rest.

    Members paste an API key from OpenAI, DeepSeek or Qwen; storing it in a
    provider setting would put plaintext on the row, so the key is sealed and
    referenced as ``member://<id>``. The gateway resolves that scheme at call
    time, which means a linked account is usable without any environment setup.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="member_credential_secrets",
    )
    sealed_value = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)

    def __str__(self) -> str:
        return f"member-secret:{self.id}"
