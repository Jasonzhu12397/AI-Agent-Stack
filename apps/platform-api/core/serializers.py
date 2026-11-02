from rest_framework import serializers

from core.models import Project, Tenant


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ["id", "name", "slug", "created_at"]
        read_only_fields = fields


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "tenant_id", "name", "slug", "created_at"]
        read_only_fields = fields
