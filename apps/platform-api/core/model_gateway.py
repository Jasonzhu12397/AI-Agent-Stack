import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings

from core.models import ModelProvider
from core.token_budget import resolve_provider_token_usage


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ModelGatewayError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ModelResponse:
    content: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    latency_ms: int
    tool_calls: list[dict] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    provider: str
    model: str
    latency_ms: int

    def as_dict(self):
        return asdict(self)


def resolve_credential(credential_ref, user=None):
    if credential_ref.startswith("member://"):
        from core.provider_accounts import resolve_member_secret

        secret = resolve_member_secret(credential_ref, user)
        if not secret:
            raise ModelGatewayError(
                "MODEL_CREDENTIAL_MISSING",
                "Your saved platform credential could not be read. Link the account again.",
            )
        return secret
    if not credential_ref.startswith("env:"):
        raise ModelGatewayError(
            "CREDENTIAL_REFERENCE_UNSUPPORTED",
            "Only environment and saved member credentials are enabled.",
        )
    variable_name = credential_ref.removeprefix("env:")
    secret = os.getenv(variable_name)
    if not secret:
        raise ModelGatewayError(
            "MODEL_CREDENTIAL_MISSING",
            "The configured model credential is not available to the runtime.",
        )
    return secret


def _is_local_http_provider(provider):
    parsed = urlsplit(provider.base_url)
    allowed_hosts = {
        host.strip().lower()
        for host in getattr(settings, "LOCAL_MODEL_ALLOWED_HOSTS", [])
        if host.strip()
    }
    return (
        parsed.scheme == "http"
        and bool(parsed.hostname)
        and parsed.hostname.lower() in allowed_hosts
    )


class ModelGateway:
    def __init__(self, guard=None, account=None):
        self._guard = guard
        self._account = account
        #: Owner of the linked account, so a sealed member credential is only
        #: readable by the member who typed it.
        self._account_user = getattr(getattr(account, "link", None), "user", None)

    def _guard_for(self, provider):
        """Pick the billed pool for this provider.

        Account providers spend the member's free allowance, everything else
        spends the tenant's purchased balance, so a deployment never has to
        guess which ledger a call belongs to.
        """

        if getattr(provider, "auth_mode", "") == ModelProvider.AuthMode.USER_ACCOUNT:
            return self._require_account()
        return self._require_guard()

    def _require_account(self):
        account = self._account
        if account is None or not getattr(account, "authorized", False):
            raise ModelGatewayError(
                "PROVIDER_ACCOUNT_NOT_LINKED",
                "Link your own account on this platform before calling its models.",
                retryable=False,
            )
        return account

    def _require_guard(self):
        if self._guard is None or not getattr(self._guard, "authorized", False):
            raise ModelGatewayError(
                "TOKEN_GUARD_REQUIRED",
                "A token budget guard must be attached before issuing a provider call.",
                retryable=False,
            )
        return self._guard

    def complete(
        self,
        model_config,
        messages,
        max_tokens=None,
        timeout_seconds=30,
        tools=None,
        tool_choice=None,
        idempotency_key=None,
    ):
        if not isinstance(messages, list) or not messages:
            raise ModelGatewayError("INVALID_MESSAGES", "At least one chat message is required.")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "system",
                "user",
                "assistant",
                "tool",
            }:
                raise ModelGatewayError(
                    "INVALID_MESSAGES",
                    "Messages must contain a supported role.",
                )
            if message.get("role") == "assistant" and message.get("tool_calls"):
                if (
                    message.get("content") is not None
                    and not isinstance(message.get("content"), str)
                ) or not isinstance(message.get("tool_calls"), list):
                    raise ModelGatewayError(
                        "INVALID_MESSAGES",
                        "Assistant tool-call messages must contain a tool_calls array.",
                    )
            elif not isinstance(message.get("content"), str):
                raise ModelGatewayError(
                    "INVALID_MESSAGES",
                    "Message content must be a string unless the assistant requests tools.",
                )
        if tools is not None:
            if not isinstance(tools, list) or len(tools) > 64:
                raise ModelGatewayError(
                    "INVALID_TOOLS",
                    "Tools must be a list of at most 64 items.",
                )
            if tools and "tools" not in (model_config.capabilities or []):
                raise ModelGatewayError(
                    "MODEL_CAPABILITY_MISSING",
                    "The selected model is not configured for tool calling.",
                )
            if tool_choice not in (None, "auto", "none", "required"):
                raise ModelGatewayError("INVALID_TOOL_CHOICE", "Unsupported tool_choice value.")
        elif tool_choice is not None:
            raise ModelGatewayError("INVALID_TOOL_CHOICE", "tool_choice requires a tools list.")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1
        ):
            raise ModelGatewayError("INVALID_MAX_TOKENS", "max_tokens must be a positive integer.")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ModelGatewayError("INVALID_TIMEOUT", "timeout_seconds must be positive.")
        provider = model_config.provider
        credential_ref = provider.credential_ref
        if getattr(provider, "auth_mode", "") == ModelProvider.AuthMode.USER_ACCOUNT:
            account = self._require_account()
            credential_ref = account.credential_ref
        if not provider.enabled or not model_config.enabled:
            raise ModelGatewayError("MODEL_DISABLED", "The selected model is disabled.")
        effective_max_tokens = min(
            max_tokens or model_config.max_output_tokens,
            model_config.max_output_tokens,
        )
        request_bytes = len(
            json.dumps(
                {"messages": messages, "tools": tools or []},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if request_bytes > 256 * 1024:
            raise ModelGatewayError(
                "MODEL_REQUEST_CONTEXT_TOO_LARGE",
                "The serialized model request exceeded the 256 KiB context limit.",
            )
        estimated_input_tokens = (request_bytes + 1) // 2
        context_window = model_config.context_window
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window < 1
            or estimated_input_tokens + effective_max_tokens > context_window
        ):
            raise ModelGatewayError(
                "MODEL_CONTEXT_WINDOW_EXCEEDED",
                "The estimated prompt and reserved output exceed the model context window.",
            )
        guard = self._guard_for(provider)
        requested_tokens = estimated_input_tokens + effective_max_tokens
        reservation = guard.reserve(
            idempotency_key=idempotency_key,
            requested_tokens=requested_tokens,
            call_type="complete",
            model_config=model_config,
        )
        started = time.monotonic()
        try:
            if provider.provider_type == ModelProvider.ProviderType.FAKE:
                content = provider.config.get("response", "Fake model response")
                if not isinstance(content, str):
                    raise ModelGatewayError(
                        "INVALID_FAKE_RESPONSE",
                        "Fake provider response must be a string.",
                    )
                resolved_input, resolved_output = 0, 0
                response = ModelResponse(
                    content=content,
                    input_tokens=resolved_input,
                    output_tokens=resolved_output,
                    provider=provider.name,
                    model=model_config.model_name,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            else:
                if provider.provider_type != ModelProvider.ProviderType.OPENAI_COMPATIBLE:
                    raise ModelGatewayError(
                        "PROVIDER_UNSUPPORTED", "The provider type is not supported."
                    )
                if urlsplit(provider.base_url).scheme == "http" and not _is_local_http_provider(
                    provider
                ):
                    raise ModelGatewayError(
                        "LOCAL_MODEL_HOST_NOT_ALLOWED",
                        "HTTP model endpoints must use an explicitly allowlisted local model host.",
                    )
                payload = self._post_json(
                    provider,
                    "chat/completions",
                    {
                        "model": model_config.model_name,
                        "messages": messages,
                        "max_tokens": effective_max_tokens,
                        **({"tools": tools, "tool_choice": tool_choice or "auto"} if tools else {}),
                    },
                    timeout_seconds,
                    credential_ref=credential_ref,
                )
                try:
                    message = payload["choices"][0]["message"]
                    content = message.get("content") or ""
                    tool_calls = message.get("tool_calls") or []
                    usage = payload.get("usage") or {}
                    if (
                        not isinstance(content, str)
                        or not isinstance(tool_calls, list)
                        or len(tool_calls) > 16
                    ):
                        raise TypeError("message content or tool_calls has an invalid type")
                    if tool_calls and not tools:
                        raise TypeError("provider returned tool calls when no tools were offered")
                    if not isinstance(usage, dict):
                        raise TypeError("usage must be an object")
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                    if (
                        not isinstance(input_tokens, int)
                        or isinstance(input_tokens, bool)
                        or input_tokens < 0
                        or input_tokens > request_bytes
                        or not isinstance(output_tokens, int)
                        or isinstance(output_tokens, bool)
                        or output_tokens < 0
                        or output_tokens > effective_max_tokens
                    ):
                        raise ValueError("provider token usage is outside the requested bounds")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ModelGatewayError(
                        "MODEL_PROVIDER_INVALID_RESPONSE",
                        "The model provider returned an invalid chat completion.",
                    ) from exc
                resolved_input, resolved_output = resolve_provider_token_usage(
                    provider_type=provider.provider_type,
                    reported_input_tokens=input_tokens,
                    reported_output_tokens=output_tokens,
                    input_token_ceiling=estimated_input_tokens,
                    output_token_ceiling=effective_max_tokens,
                )
                response = ModelResponse(
                    content=content,
                    input_tokens=resolved_input,
                    output_tokens=resolved_output,
                    provider=provider.name,
                    model=model_config.model_name,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    tool_calls=tool_calls,
                )
        except BaseException:
            guard.release(reservation=reservation)
            raise
        guard.settle(
            reservation=reservation,
            consumed_tokens=resolved_input + resolved_output,
        )
        return response

    def embed(self, model_config, inputs, timeout_seconds=45, idempotency_key=None):
        if (
            not isinstance(inputs, list)
            or not inputs
            or any(not isinstance(item, str) or not item.strip() for item in inputs)
        ):
            raise ModelGatewayError(
                "INVALID_EMBEDDING_INPUT",
                "Embedding input must be a non-empty list of non-empty strings.",
            )
        if (
            len(inputs) > 128
            or any(len(item) > 20000 for item in inputs)
            or sum(len(item.encode("utf-8")) for item in inputs) > 256 * 1024
        ):
            raise ModelGatewayError(
                "EMBEDDING_INPUT_LIMIT",
                "Embedding requests are limited to 128 texts, 20,000 characters each, "
                "and 256 KiB total UTF-8 input.",
            )
        provider = model_config.provider
        credential_ref = provider.credential_ref
        if getattr(provider, "auth_mode", "") == ModelProvider.AuthMode.USER_ACCOUNT:
            account = self._require_account()
            credential_ref = account.credential_ref
        if not provider.enabled or not model_config.enabled:
            raise ModelGatewayError("MODEL_DISABLED", "The selected model is disabled.")
        if "embeddings" not in (model_config.capabilities or []):
            raise ModelGatewayError(
                "MODEL_CAPABILITY_MISSING",
                "The selected model is not configured for embeddings.",
            )
        if provider.provider_type != ModelProvider.ProviderType.OPENAI_COMPATIBLE:
            raise ModelGatewayError(
                "PROVIDER_UNSUPPORTED",
                "The selected provider does not support embeddings.",
            )
        if urlsplit(provider.base_url).scheme == "http" and not _is_local_http_provider(provider):
            raise ModelGatewayError(
                "LOCAL_MODEL_HOST_NOT_ALLOWED",
                "HTTP model endpoints must use an explicitly allowlisted local model host.",
            )
        guard = self._require_guard()
        requested_tokens = max(1, sum(len(item.encode("utf-8")) for item in inputs) // 2)
        reservation = guard.reserve(
            idempotency_key=idempotency_key,
            requested_tokens=requested_tokens,
            call_type="embed",
            model_config=model_config,
        )
        started = time.monotonic()
        try:
            payload = self._post_json(
                provider,
                "embeddings",
                {"model": model_config.model_name, "input": inputs},
                timeout_seconds,
                credential_ref=credential_ref,
            )
            try:
                rows = payload["data"]
                rows = sorted(rows, key=lambda row: int(row["index"]))
                vectors = [row["embedding"] for row in rows]
                if len(vectors) != len(inputs):
                    raise ValueError("embedding count mismatch")
                dimensions = {len(vector) for vector in vectors}
                if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) <= 0:
                    raise ValueError("embedding dimensions mismatch")
                normalized = []
                for vector in vectors:
                    if not isinstance(vector, list):
                        raise TypeError("embedding must be an array")
                    values = [float(value) for value in vector]
                    if any(not math.isfinite(value) for value in values):
                        raise ValueError("embedding contains non-finite values")
                    normalized.append(values)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelGatewayError(
                    "MODEL_PROVIDER_INVALID_EMBEDDING",
                    "The model provider returned invalid embeddings.",
                ) from exc
        except BaseException:
            guard.release(reservation=reservation)
            raise
        # Embedding responses carry no usage report, so the reserved input
        # estimate is charged as the settled consumption.
        guard.settle(reservation=reservation, consumed_tokens=reservation.reserved_tokens)
        return EmbeddingResponse(
            vectors=normalized,
            provider=provider.name,
            model=model_config.model_name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _post_json(self, provider, endpoint, body, timeout_seconds, credential_ref=None):
        parsed = urlsplit(provider.base_url)
        is_local_http = parsed.scheme == "http" and _is_local_http_provider(provider)
        if parsed.scheme != "https" and not is_local_http:
            raise ModelGatewayError(
                "MODEL_ENDPOINT_SCHEME_INVALID",
                "Model endpoints must use HTTPS or an allowlisted local HTTP endpoint.",
            )
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        credential_ref = credential_ref or provider.credential_ref
        if credential_ref:
            credential = resolve_credential(credential_ref, self._account_user)
            headers["Authorization"] = f"Bearer {credential}"
        elif not is_local_http:
            raise ModelGatewayError(
                "MODEL_CREDENTIAL_MISSING",
                "Remote model providers require an environment-backed credential reference.",
            )
        url = provider.base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener = build_opener(NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:
                raw_payload = response.read(16 * 1024 * 1024 + 1)
            if len(raw_payload) > 16 * 1024 * 1024:
                raise ModelGatewayError(
                    "MODEL_PROVIDER_RESPONSE_TOO_LARGE",
                    "The model provider response exceeded the 16 MiB limit.",
                )
            payload = json.loads(raw_payload.decode("utf-8"))
        except HTTPError as exc:
            raise ModelGatewayError(
                "MODEL_PROVIDER_HTTP_ERROR",
                f"The model provider returned HTTP {exc.code}.",
                retryable=exc.code >= 500 or exc.code == 429,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ModelGatewayError(
                "MODEL_PROVIDER_UNAVAILABLE",
                "The model provider could not be reached.",
                retryable=True,
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError(
                "MODEL_PROVIDER_INVALID_RESPONSE",
                "The model provider returned an invalid response.",
            ) from exc
        if not isinstance(payload, dict):
            raise ModelGatewayError(
                "MODEL_PROVIDER_INVALID_RESPONSE",
                "The model provider returned an invalid response.",
            )
        return payload
