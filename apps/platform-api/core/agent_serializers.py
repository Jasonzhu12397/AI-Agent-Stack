from rest_framework import serializers

from core.models import AgentDefinition, AgentRun, AgentVersion


class AgentDraftSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentDefinition
        fields = [
            "id",
            "tenant_id",
            "project_id",
            "name",
            "slug",
            "description",
            "draft_config",
            "current_version",
            "is_archived",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "tenant_id",
            "project_id",
            "current_version",
            "is_archived",
            "created_at",
            "updated_at",
        ]

    def validate_draft_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Draft configuration must be a JSON object.")
        if len(str(value)) > 32768:
            raise serializers.ValidationError("Draft configuration exceeds 32 KiB.")
        forbidden = {"api_key", "secret", "password", "access_token", "client_secret"}

        def contains_secret_key(item):
            if isinstance(item, dict):
                return any(
                    str(key).lower() in forbidden or contains_secret_key(child)
                    for key, child in item.items()
                )
            if isinstance(item, list):
                return any(contains_secret_key(child) for child in item)
            return False

        if contains_secret_key(value):
            raise serializers.ValidationError(
                "Credentials must use credential references, not draft configuration."
            )
        return value


class AgentVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentVersion
        fields = ["id", "agent_id", "version", "config_snapshot", "published_at"]
        read_only_fields = fields


class AgentRunSerializer(serializers.ModelSerializer):
    task_status = serializers.CharField(source="task.status", read_only=True)
    task_id = serializers.UUIDField(read_only=True)
    agent_version_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = AgentRun
        fields = [
            "id",
            "task_id",
            "agent_version_id",
            "task_status",
            "input_text",
            "output_text",
            "input_tokens",
            "output_tokens",
            "provider_name",
            "model_name",
            "latency_ms",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields
