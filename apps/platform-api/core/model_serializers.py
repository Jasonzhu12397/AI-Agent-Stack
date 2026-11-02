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


class ModelProviderWriteSerializer(serializers.ModelSerializer):
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
        if provider_type == ModelProvider.ProviderType.FAKE:
            if base_url or credential_ref:
                raise serializers.ValidationError(
                    "Fake providers cannot contain a base URL or credential reference."
                )
        elif provider_type == ModelProvider.ProviderType.OPENAI_COMPATIBLE:
            parsed = urlsplit(base_url)
            allowed_hosts = {
                host.strip().lower()
                for host in getattr(settings, "MODEL_PROVIDER_ALLOWED_HOSTS", [])
                if host.strip()
            }
            if parsed.scheme != "https" or not parsed.hostname:
                raise serializers.ValidationError(
                    {"base_url": "OpenAI-compatible providers require an HTTPS URL."}
                )
            if parsed.hostname.lower() not in allowed_hosts:
                raise serializers.ValidationError(
                    {"base_url": "Provider host is not in MODEL_PROVIDER_ALLOWED_HOSTS."}
                )
            if not re.fullmatch(r"env:[A-Z][A-Z0-9_]{0,127}", credential_ref):
                raise serializers.ValidationError(
                    {"credential_ref": "Use an environment reference such as env:OPENAI_API_KEY."}
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
        if attrs.get("max_output_tokens", 1) <= 0:
            raise serializers.ValidationError({"max_output_tokens": "Must be greater than zero."})
        return attrs
