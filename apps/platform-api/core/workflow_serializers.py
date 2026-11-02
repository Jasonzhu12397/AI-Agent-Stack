from rest_framework import serializers

from core.models import WorkflowDefinition, WorkflowRun, WorkflowStepRun, WorkflowVersion


class WorkflowDraftSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowDefinition
        fields = [
            "id", "tenant_id", "project_id", "name", "slug", "description",
            "draft_definition", "current_version", "is_archived", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "tenant_id", "project_id", "current_version", "is_archived",
            "created_at", "updated_at",
        ]

    def validate_draft_definition(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Workflow definition must be a JSON object.")
        if len(str(value)) > 131072:
            raise serializers.ValidationError("Workflow definition exceeds 128 KiB.")
        return value


class WorkflowVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowVersion
        fields = ["id", "workflow_id", "version", "definition_snapshot", "published_at"]
        read_only_fields = fields


class WorkflowStepRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStepRun
        fields = [
            "id", "node_id", "node_type", "status", "input_data", "output_data",
            "error", "attempt", "started_at", "finished_at",
        ]
        read_only_fields = fields


class WorkflowRunSerializer(serializers.ModelSerializer):
    task_status = serializers.CharField(source="task.status", read_only=True)
    task_id = serializers.UUIDField(read_only=True)
    workflow_version_id = serializers.UUIDField(read_only=True)
    steps = WorkflowStepRunSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowRun
        fields = [
            "id", "task_id", "workflow_version_id", "task_status", "input_data",
            "state", "output_data", "steps", "created_at", "finished_at",
        ]
        read_only_fields = fields
