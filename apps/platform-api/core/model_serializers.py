import re
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework import serializers

from core.models import ModelConfig, ModelProvider


class ModelProviderReadSerializer(serializers.ModelSerializer):
    credential_configured = serializers.SerializerMethodField()

    class Meta:
        model = ModelProvider
        fields = [
            "id",
            "name",
            "provider_type",
            "base_url",
            "credential_configured",
            "enabled",
            "created_at",
        ]
        read_only_fields = fields

    def get_credential_configured(self, obj):
        return bool(obj.credential_ref)


def contains_secret_key(value):
    secret_names = {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "authorization",
        "bearer_token",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in secret_names or contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(contains_secret_key(item) for item in value)
    return False


class ModelProviderWriteSerializer(serializers.ModelSerializer):
    credential_ref = serializers.CharField(required=False, allow_blank=True, max_length=200)

    class Meta:
        model = ModelProvider
        fields = ["name", "provider_type", "base_url", "credential_ref", "config"]

    def validate(self, attrs):
        provider_type = attrs.get("provider_type")
        base_url = attrs.get("base_url", "")
        credential_ref = attrs.get("credential_ref", "")
        config = attrs.get("config", {})
        if not isinstance(config, dict):
            raise serializers.ValidationError({"config": "Config must be a JSON object."})
        if contains_secret_key(config):
            raise serializers.ValidationError(
                {"config": "Secret material must be stored through credential_ref, not config."}
            )
        if provider_type == ModelProvider.ProviderType.FAKE:
            if base_url or credential_ref:
                raise serializers.ValidationError(
                    "Fake providers cannot contain a base URL or credential reference."
                )
        elif provider_type == ModelProvider.ProviderType.OPENAI_COMPATIBLE:
            parsed = urlsplit(base_url)
            remote_hosts = {
                host.strip().lower()
                for host in getattr(settings, "MODEL_PROVIDER_ALLOWED_HOSTS", [])
                if host.strip()
            }
            local_hosts = {
                host.strip().lower()
                for host in getattr(settings, "LOCAL_MODEL_ALLOWED_HOSTS", [])
                if host.strip()
            }
            host = (parsed.hostname or "").lower()
            local_http = parsed.scheme == "http" and host in local_hosts
            remote_https = parsed.scheme == "https" and host in remote_hosts
            if not parsed.hostname or not (local_http or remote_https):
                raise serializers.ValidationError(
                    {
                        "base_url": (
                            "Use HTTPS with MODEL_PROVIDER_ALLOWED_HOSTS for remote providers, "
                            "or HTTP with LOCAL_MODEL_ALLOWED_HOSTS for local model servers."
                        )
                    }
                )
            if (
                parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (remote_https and parsed.port not in (None, 443))
            ):
                raise serializers.ValidationError(
                    {"base_url": "Provider URL contains unsupported components."}
                )
            if local_http and credential_ref and not re.fullmatch(
                r"env:[A-Z][A-Z0-9_]{0,127}", credential_ref
            ):
                raise serializers.ValidationError(
                    {"credential_ref": "Use an environment reference such as env:LOCAL_MODEL_KEY."}
                )
            if remote_https and not re.fullmatch(
                r"env:[A-Z][A-Z0-9_]{0,127}", credential_ref
            ):
                raise serializers.ValidationError(
                    {"credential_ref": "Remote providers require an env: credential reference."}
                )
        else:
            raise serializers.ValidationError({"provider_type": "Unsupported provider type."})
        return attrs


class ModelConfigSerializer(serializers.ModelSerializer):
    provider_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = ModelConfig
        fields = [
            "id",
            "provider_id",
            "model_name",
            "display_name",
            "capabilities",
            "context_window",
            "max_output_tokens",
            "input_cost_per_million",
            "output_cost_per_million",
            "version",
            "enabled",
            "created_at",
        ]
        read_only_fields = ["id", "provider_id", "version", "created_at"]

    def validate_capabilities(self, value):
        allowed = {"chat", "json", "tools", "vision", "embeddings"}
        if not isinstance(value, list) or not set(value).issubset(allowed):
            raise serializers.ValidationError("Capabilities contain unsupported values.")
        return value

    def validate(self, attrs):
        if attrs.get("context_window", 1) <= 0:
            raise serializers.ValidationError({"context_window": "Must be greater than zero."})
        max_output_tokens = attrs.get("max_output_tokens", 1)
        if max_output_tokens <= 0:
            raise serializers.ValidationError({"max_output_tokens": "Must be greater than zero."})
        if max_output_tokens > attrs.get("context_window", 32768):
            raise serializers.ValidationError(
                {"max_output_tokens": "Cannot exceed the model context window."}
            )
        for field in ("input_cost_per_million", "output_cost_per_million"):
            if attrs.get(field, 0) < 0:
                raise serializers.ValidationError({field: "Cost cannot be negative."})
        return attrs
