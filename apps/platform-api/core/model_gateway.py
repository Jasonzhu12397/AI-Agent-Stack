import json
import os
import time
from dataclasses import asdict, dataclass
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core.models import ModelConfig, ModelProvider


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

    def as_dict(self):
        return asdict(self)


def resolve_credential(credential_ref):
    if not credential_ref.startswith("env:"):
        raise ModelGatewayError(
            "CREDENTIAL_REFERENCE_UNSUPPORTED",
            "Only environment-backed credential references are enabled.",
        )
    variable_name = credential_ref.removeprefix("env:")
    secret = os.getenv(variable_name)
    if not secret:
        raise ModelGatewayError(
            "MODEL_CREDENTIAL_MISSING",
            "The configured model credential is not available to the runtime.",
        )
    return secret


class ModelGateway:
    def complete(
        self,
        model_config: ModelConfig,
        messages,
        max_tokens=None,
        timeout_seconds=30,
    ):
        if not isinstance(messages, list) or not messages:
            raise ModelGatewayError("INVALID_MESSAGES", "At least one chat message is required.")
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
            ):
                raise ModelGatewayError(
                    "INVALID_MESSAGES",
                    "Messages must contain a supported role and string content.",
                )
        provider = model_config.provider
        if not provider.enabled or not model_config.enabled:
            raise ModelGatewayError("MODEL_DISABLED", "The selected model is disabled.")
        started = time.monotonic()
        if provider.provider_type == ModelProvider.ProviderType.FAKE:
            content = provider.config.get("response", "Fake model response")
            if not isinstance(content, str):
                raise ModelGatewayError(
                    "INVALID_FAKE_RESPONSE",
                    "Fake provider response must be a string.",
                )
            elapsed = int((time.monotonic() - started) * 1000)
            return ModelResponse(
                content=content,
                input_tokens=0,
                output_tokens=0,
                provider=provider.name,
                model=model_config.model_name,
                latency_ms=elapsed,
            )
        if provider.provider_type != ModelProvider.ProviderType.OPENAI_COMPATIBLE:
            raise ModelGatewayError("PROVIDER_UNSUPPORTED", "The provider type is not supported.")
        return self._openai_compatible(
            model_config,
            messages,
            started,
            max_tokens,
            timeout_seconds,
        )

    def _openai_compatible(
        self,
        model_config,
        messages,
        started,
        max_tokens,
        timeout_seconds,
    ):
        provider = model_config.provider
        credential = resolve_credential(provider.credential_ref)
        url = provider.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(
            {
                "model": model_config.model_name,
                "messages": messages,
                "max_tokens": min(
                    max_tokens or model_config.max_output_tokens,
                    model_config.max_output_tokens,
                ),
            }
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            opener = build_opener(NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
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
        try:
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            if not isinstance(content, str):
                raise TypeError("content must be a string")
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "MODEL_PROVIDER_INVALID_RESPONSE",
                "The model provider returned an invalid chat completion.",
            ) from exc
        return ModelResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=provider.name,
            model=model_config.model_name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
