"""Account authorization without a copied key (OAuth 2.0 + PKCE).

Some platforms let a third-party app ask the account owner for permission and
receive an API key in return; OpenRouter is one. That is the only path where
"authorize the account" alone is enough, because the platform issues the
credential instead of asking the member to copy one. Platforms that do not
offer such a grant stay on the paste-a-key flow, and the catalogue says which
is which.

The flow is the standard public-client one: a per-attempt verifier is sealed
server-side, only its hash travels in the authorization URL, and the code is
redeemed exactly once at the platform's key endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from core import provider_accounts

#: How long a member has to finish the authorization page before the attempt
#: expires. Long enough for a login with a second factor, short enough that an
#: abandoned verifier stops being a usable secret.
STATE_TTL_SECONDS = 600
#: The authorization code is single-use, so the exchange must not hang.
EXCHANGE_TIMEOUT_SECONDS = 20


class OAuthFlowError(Exception):
    """The authorization attempt was invalid, expired or refused."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    state: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def code_challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def preset_oauth(preset_key: str) -> dict:
    preset = provider_accounts.PROVIDER_PRESETS.get(preset_key)
    if not preset or not preset.get("oauth"):
        raise OAuthFlowError(
            "OAUTH_PRESET_UNSUPPORTED",
            f"{preset_key} does not offer an authorization flow; paste an API key instead.",
            http_status=400,
        )
    return preset["oauth"]


def start_flow(*, user, tenant, preset_key: str, redirect_uri: str) -> AuthorizationStart:
    oauth = preset_oauth(preset_key)
    if not redirect_uri.startswith(("http://", "https://")):
        raise OAuthFlowError(
            "OAUTH_REDIRECT_INVALID",
            "The callback URL must be an absolute http(s) address.",
        )
    verifier = secrets.token_urlsafe(64)
    challenge = code_challenge_for(verifier)
    state = secrets.token_urlsafe(32)
    from core.field_crypto import seal

    with transaction.atomic():
        provider_accounts.ProviderOAuthState.objects.create(
            user=user,
            tenant=tenant,
            preset_key=preset_key,
            state=state,
            sealed_verifier=seal(verifier, "provider-oauth-verifier"),
            redirect_uri=redirect_uri,
            expires_at=timezone.now() + timedelta(seconds=STATE_TTL_SECONDS),
        )
    separator = "&" if "?" in oauth["authorize_url"] else "?"
    url = (
        f"{oauth['authorize_url']}{separator}"
        f"callback_url={quote(redirect_uri, safe='')}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )
    return AuthorizationStart(authorization_url=url, state=state)


def post_key_request(url: str, payload: dict, timeout: float = EXCHANGE_TIMEOUT_SECONDS) -> dict:
    """Redeem the authorization code. Split out so tests never touch the wire."""

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OAuthFlowError(
            "OAUTH_EXCHANGE_REFUSED",
            f"The platform refused the authorization code (HTTP {exc.code}). Try again.",
            http_status=400,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthFlowError(
            "OAUTH_EXCHANGE_UNREACHABLE",
            "The platform could not be reached to finish the authorization.",
            http_status=502,
        ) from exc


def complete_flow(*, user, tenant, preset_key: str, code: str, state: str) -> tuple:
    preset_oauth(preset_key)
    if not (code or "").strip() or not (state or "").strip():
        raise OAuthFlowError(
            "OAUTH_CODE_MISSING", "The authorization response is missing its code or state."
        )
    from core.field_crypto import open_sealed

    attempt = provider_accounts.ProviderOAuthState.objects.filter(
        user=user, preset_key=preset_key, state=state, consumed_at__isnull=True
    ).first()
    if attempt is None:
        raise OAuthFlowError(
            "OAUTH_STATE_UNKNOWN",
            "This authorization attempt is unknown or already used. Start again.",
        )
    if attempt.expires_at <= timezone.now():
        raise OAuthFlowError(
            "OAUTH_STATE_EXPIRED", "The authorization attempt expired. Start again."
        )
    verifier = open_sealed(attempt.sealed_verifier, "provider-oauth-verifier")
    oauth = preset_oauth(preset_key)
    payload = {"code": code.strip(), "code_verifier": verifier}
    if oauth.get("key_label"):
        payload["key_label"] = oauth["key_label"]
    document = post_key_request(oauth["key_endpoint"], payload)
    key = document.get("key") if isinstance(document, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise OAuthFlowError(
            "OAUTH_KEY_MISSING",
            "The platform did not return a key for this authorization.",
            http_status=502,
        )

    with transaction.atomic():
        provider = provider_accounts.ensure_account_provider(
            tenant=tenant, user=user, preset_key=preset_key
        )
        link = provider_accounts.link_account(
            user=user,
            provider=provider,
            credential_ref=key.strip(),
            account_label=(
                (document.get("label") or "")[: provider_accounts.MAX_ACCOUNT_LABEL_LENGTH]
                if isinstance(document, dict)
                else ""
            ),
        )
        attempt.consumed_at = timezone.now()
        attempt.save(update_fields=["consumed_at", "updated_at"])
    return provider, link
