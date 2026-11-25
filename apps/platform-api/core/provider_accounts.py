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
from django.db.models import Q
from django.utils import timezone

from core.models import ModelProvider, ProviderAccountLink, ProviderAccountUsage

#: Model platforms a member may attach their own account to. ``base_url`` is
#: the OpenAI-compatible endpoint each platform exposes, so one transport
#: serves all of them and a new platform is a line in this table.
PROVIDER_PRESETS: dict[str, dict] = {
    "openai": {
        "console_url": "https://platform.openai.com/api-keys",
        "platform_env": "PLATFORM_OPENAI_KEY",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"model_name": "gpt-5.6", "display_name": "GPT-5.6"},
            {"model_name": "gpt-5.6-terra", "display_name": "GPT-5.6 Terra"},
            {"model_name": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna"},
        ],
    },
    "doubao": {
        "console_url": "https://console.volcengine.com/ark",
        "platform_env": "PLATFORM_DOUBAO_KEY",
        "label": "豆包 Doubao",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [
            {"model_name": "doubao-seed-2-1-pro-260915", "display_name": "Doubao Seed 2.1 Pro"},
            {"model_name": "doubao-seed-2.0-pro", "display_name": "Doubao Seed 2.0 Pro"},
            {"model_name": "doubao-seed-2.0-lite", "display_name": "Doubao Seed 2.0 Lite"},
        ],
    },
    "google": {
        "console_url": "https://aistudio.google.com/app/apikey",
        "platform_env": "PLATFORM_GOOGLE_KEY",
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": [
            {"model_name": "gemini-3.6-flash", "display_name": "Gemini 3.6 Flash"},
            {"model_name": "gemini-3.1-pro-preview", "display_name": "Gemini 3.1 Pro"},
            {"model_name": "gemini-3.5-flash-lite", "display_name": "Gemini 3.5 Flash-Lite"},
        ],
    },
    "deepseek": {
        "console_url": "https://platform.deepseek.com/api_keys",
        "platform_env": "PLATFORM_DEEPSEEK_KEY",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"model_name": "deepseek-v4.1-flash", "display_name": "DeepSeek V4.1 Flash"},
            {"model_name": "deepseek-v4-pro", "display_name": "DeepSeek V4 Pro"},
        ],
    },
    "zhipu": {
        "console_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "platform_env": "PLATFORM_ZHIPU_KEY",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"model_name": "glm-5.4", "display_name": "GLM-5.4"},
            {"model_name": "glm-5.3-flash", "display_name": "GLM-5.3 Flash"},
        ],
    },
    "moonshot": {
        "console_url": "https://platform.moonshot.cn/console/api-keys",
        "platform_env": "PLATFORM_MOONSHOT_KEY",
        "label": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            {"model_name": "kimi-k3.1-pro", "display_name": "Kimi K3.1 Pro"},
            {"model_name": "kimi-k3", "display_name": "Kimi K3"},
        ],
    },
    "minimax": {
        "console_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "platform_env": "PLATFORM_MINIMAX_KEY",
        "label": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "models": [
            {"model_name": "MiniMax-M3", "display_name": "MiniMax M3"},
            {"model_name": "MiniMax-M2.7", "display_name": "MiniMax M2.7"},
        ],
    },
    "qwen": {
        "console_url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "platform_env": "PLATFORM_QWEN_KEY",
        "label": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"model_name": "qwen3.8-max", "display_name": "Qwen3.8 Max"},
            {"model_name": "qwen3.8-plus", "display_name": "Qwen3.8 Plus"},
            {"model_name": "qwen3.8-flash", "display_name": "Qwen3.8 Flash"},
        ],
    },
    "openrouter": {
        "console_url": "https://openrouter.ai/keys",
        "platform_env": "PLATFORM_OPENROUTER_KEY",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {
                "model_name": "openai/gpt-5.6-terra",
                "display_name": "GPT-5.6 Terra (via OpenRouter)",
            },
            {"model_name": "openai/gpt-5.6-luna", "display_name": "GPT-5.6 Luna (via OpenRouter)"},
            {
                "model_name": "deepseek/deepseek-v4.1-flash",
                "display_name": "DeepSeek V4.1 Flash (via OpenRouter)",
            },
            {"model_name": "minimax/minimax-m3", "display_name": "MiniMax M3 (via OpenRouter)"},
        ],
        #: OpenRouter is the platform that can authorize an account and issue
        #: the key itself, so the member never copies one.
        "oauth": {
            "authorize_url": "https://openrouter.ai/auth",
            "key_endpoint": "https://openrouter.ai/api/v1/auth/keys",
            "key_label": "AI Agent Studio",
        },
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


def provider_scope_q(tenant, user) -> Q:
    """Providers a member may see: tenant-wide rows plus their own account rows.

    A ``user_account`` provider belongs to the member who created it. Sharing
    such a row across the tenant would show every member's linked platforms in
    Model Manager and let them bind models they cannot actually bill, so only
    its creator may see it. Tenant-secret, platform-pool and global providers
    stay tenant-wide.
    """

    return (
        Q(tenant=tenant) | Q(tenant__isnull=True)
    ) & (~Q(auth_mode=ModelProvider.AuthMode.USER_ACCOUNT) | Q(created_by=user))


def model_config_scope_q(tenant, user) -> Q:
    """The same visibility rule expressed for ``ModelConfig`` lookups."""

    return (
        Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True)
    ) & (
        ~Q(provider__auth_mode=ModelProvider.AuthMode.USER_ACCOUNT)
        | Q(provider__created_by=user)
    )


def visible_providers(tenant, user):
    """Provider queryset for a member, with their personal rows kept private."""

    return ModelProvider.objects.filter(provider_scope_q(tenant, user)).distinct()


def catalogue() -> list[dict]:
    """Every attachable platform with the models it publishes."""

    return [
        {
            "key": key,
            "label": preset["label"],
            "base_url": preset["base_url"],
            #: The platform page that issues an API key, so the dialog can send
            #: the member to the vendor UI instead of leaving them to guess.
            "console_url": preset.get("console_url", ""),
            "models": [dict(item) for item in preset["models"]],
            #: True when the platform can grant access itself, so the console
            #: offers "Authorize" instead of asking for a pasted key.
            "oauth": bool(preset.get("oauth")),
            #: Operator-held credential for the shared pool. When it is present
            #: the models work for every signed-in member with no key at all.
            "platform_env": preset.get("platform_env", ""),
            "platform_configured": bool(os.getenv(preset.get("platform_env", ""))
                                        if preset.get("platform_env") else False),
        }
        for key, preset in PROVIDER_PRESETS.items()
    ]


def is_platform_provider(provider) -> bool:
    return provider.auth_mode == ModelProvider.AuthMode.PLATFORM_POOL


def platform_credential_reference(preset_key: str) -> str:
    preset = PROVIDER_PRESETS.get(preset_key)
    if preset is None or not preset.get("platform_env"):
        return ""
    return "env://" + preset["platform_env"]


def platform_env_configured(preset_key: str) -> bool:
    preset = PROVIDER_PRESETS.get(preset_key)
    variable = preset.get("platform_env") if preset else ""
    return bool(variable and os.getenv(variable))


def sync_platform_providers(*, tenant, user) -> list[ModelProvider]:
    """Create the shared-pool rows for every platform whose key this runtime holds.

    The rows are data, not code: an operator who sets ``PLATFORM_OPENAI_KEY``
    gets working models after a plain SSO login, and one who does not gets no
    phantom provider that fails at call time.
    """

    created: list[ModelProvider] = []
    for preset_key in PROVIDER_PRESETS:
        if not platform_env_configured(preset_key):
            continue
        existing = ModelProvider.objects.filter(
            tenant=tenant,
            preset_key=preset_key,
            auth_mode=ModelProvider.AuthMode.PLATFORM_POOL,
        ).first()
        if existing is not None:
            created.append(existing)
            continue
        preset = PROVIDER_PRESETS[preset_key]
        name = f"{preset['label']} (platform)"
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
                auth_mode=ModelProvider.AuthMode.PLATFORM_POOL,
                preset_key=preset_key,
                base_url=preset["base_url"],
                credential_ref=platform_credential_reference(preset_key),
            )
            seed_preset_models(provider)
        created.append(provider)
    return created


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
def verify_account_credential(provider, credential: str) -> dict:
    """Check a pasted key against the platform before storing it.

    A linked account that can never authenticate is worse than no link: the
    console says "connected" and every chat then fails with HTTP 401. The
    OpenAI-compatible transport every preset uses exposes ``GET /models``,
    then confirms the key may actually run inference with a one-word
    completion. A rejection is final; a platform that hides an endpoint
    (404/405) is reported as unverified rather than blocked.
    """

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, build_opener

    value = (credential or "").strip()
    if not value:
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_CREDENTIAL_REQUIRED",
            "A credential reference for your own account is required.",
        )
    if any(character.isspace() for character in value):
        raise ProviderAccountError(
            "PROVIDER_ACCOUNT_CREDENTIAL_INVALID",
            "The API key contains whitespace; copy it again without spaces or newlines.",
        )
    if "://" in value or value.startswith("env:"):
        # Operator-injected references are resolved at call time; validating
        # here would turn a missing process variable into a link-time outage.
        return {"verified": None, "reason": "reference"}
    url = provider.base_url.rstrip("/") + "/models"
    request = Request(
        url,
        headers={"Authorization": f"Bearer {value}", "Accept": "application/json"},
    )
    try:
        from core.model_gateway import NoRedirectHandler

        with build_opener(NoRedirectHandler()).open(request, timeout=8) as response:
            response.read(1024 * 1024)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProviderAccountError(
                "PROVIDER_ACCOUNT_CREDENTIAL_REJECTED",
                f"{provider.name} rejected this API key (HTTP {exc.code}). "
                "Copy a fresh key from the platform's API page and paste it again.",
                http_status=400,
            ) from exc
        if exc.code in {404, 405}:
            return {"verified": False, "reason": f"endpoint_{exc.code}"}
        return {"verified": False, "reason": f"http_{exc.code}"}
    except (URLError, TimeoutError, OSError):
        # A network hiccup should not prevent a member from saving a key that
        # they can verify later from the model test.
        return {"verified": False, "reason": "unreachable"}
    return _probe_chat_completion(provider, value)


def _probe_chat_completion(provider, credential):
    """Verify the key can actually run inference, not merely list models.

    Restricted project keys can pass ``GET /models`` and still be denied on
    ``POST /chat/completions``; linking such a key produced a green "connected"
    card and a 401 on the first chat turn. The probe uses the preset's first
    model with one output token, so a chat-permission failure is caught before
    the credential is stored.
    """

    import json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, build_opener

    preset = PROVIDER_PRESETS.get(provider.preset_key) or {}
    models = preset.get("models") or []
    if not models:
        return {"verified": False, "reason": "chat_unprobed"}
    body = {
        "model": models[0]["model_name"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
    }
    request = Request(
        provider.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        from core.model_gateway import NoRedirectHandler

        with build_opener(NoRedirectHandler()).open(request, timeout=10) as response:
            response.read(1024 * 1024)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProviderAccountError(
                "PROVIDER_ACCOUNT_CREDENTIAL_REJECTED",
                f"{provider.name} rejected this API key for chat completion "
                f"(HTTP {exc.code}). The key may be restricted to model listing; "
                "create a key with chat permission and paste it again.",
                http_status=400,
            ) from exc
        return {"verified": False, "reason": f"chat_{exc.code}"}
    except (URLError, TimeoutError, OSError):
        return {"verified": False, "reason": "chat_unreachable"}
    return {"verified": True, "reason": "chat_completion"}


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
    # A fresh key is also the moment to pick up the platform's current models.
    refresh_preset_models(provider)
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


def refresh_preset_models(provider) -> dict:
    """Bring a preset-backed provider up to the catalogue's current models.

    Platform catalogues move; a member who authorized last quarter should not
    keep calling a retired model. Models the catalogue no longer lists are
    disabled when they still look like our own seeding (chat/tools, zero price),
    so a model an operator added by hand keeps working.
    """

    from core.models import ModelConfig

    preset = PROVIDER_PRESETS.get(provider.preset_key) or {"models": []}
    wanted = {item["model_name"]: item["display_name"] for item in preset["models"]}
    added = 0
    for model_name, display_name in wanted.items():
        _, made = ModelConfig.objects.get_or_create(
            provider=provider,
            model_name=model_name,
            version=1,
            defaults={
                "display_name": display_name,
                "capabilities": ["chat", "tools"],
                "input_cost_per_million": 0,
                "output_cost_per_million": 0,
            },
        )
        added += int(made)
    retired = 0
    for model in provider.models.all():
        if model.model_name in wanted or not model.enabled:
            continue
        if list(model.capabilities or []) != ["chat", "tools"]:
            continue
        if model.input_cost_per_million or model.output_cost_per_million:
            continue
        model.enabled = False
        model.save(update_fields=["enabled"])
        retired += 1
    return {"added": added, "retired": retired}


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
        # Only the presence of a credential is exposed; the key itself never
        # leaves the sealed member secret.
        "credential_configured": bool(link.credential_ref),
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
