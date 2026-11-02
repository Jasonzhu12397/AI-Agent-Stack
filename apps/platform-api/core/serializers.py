from rest_framework import serializers

from core.models import Project, Tenant


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ["id", "name", "slug", "created_at"]
        read_only_fields = fields


class ProjectSerializer(serializers.ModelSerializer):
    tenant_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Project
        fields = ["id", "tenant_id", "name", "slug", "created_at"]
        read_only_fields = ["id", "tenant_id", "created_at"]

    def validate_slug(self, value):
        tenant_id = self.context["view"].kwargs["tenant_id"]
        if Project.objects.filter(tenant_id=tenant_id, slug=value).exists():
            raise serializers.ValidationError("Project slug already exists in this tenant.")
        return value
