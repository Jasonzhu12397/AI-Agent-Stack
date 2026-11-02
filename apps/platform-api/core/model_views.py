from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.model_gateway import ModelGateway, ModelGatewayError
from core.model_serializers import (
    ModelConfigSerializer,
    ModelProviderReadSerializer,
    ModelProviderWriteSerializer,
)
from core.models import ModelConfig, ModelProvider, Tenant, TenantMembership


def get_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def is_tenant_admin(request, tenant):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()


class ModelProviderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = get_tenant(request, tenant_id)
        providers = ModelProvider.objects.filter(
            Q(tenant=tenant) | Q(tenant__isnull=True),
            enabled=True,
        )
        return Response(ModelProviderReadSerializer(providers, many=True).data)

    def post(self, request, tenant_id):
        tenant = get_tenant(request, tenant_id)
        if not is_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ModelProviderWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provider = serializer.save(tenant=tenant, created_by=request.user)
        return Response(
            ModelProviderReadSerializer(provider).data,
            status=status.HTTP_201_CREATED,
        )


class ModelConfigListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_provider(self, request, tenant, provider_id):
        return get_object_or_404(
            ModelProvider.objects.filter(
                Q(tenant=tenant) | Q(tenant__isnull=True),
                enabled=True,
            ),
            pk=provider_id,
        )

    def get(self, request, tenant_id, provider_id):
        tenant = get_tenant(request, tenant_id)
        provider = self.get_provider(request, tenant, provider_id)
        configs = ModelConfig.objects.filter(provider=provider, enabled=True)
        return Response(ModelConfigSerializer(configs, many=True).data)

    def post(self, request, tenant_id, provider_id):
        tenant = get_tenant(request, tenant_id)
        if not is_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        provider = self.get_provider(request, tenant, provider_id)
        if provider.tenant_id != tenant.id:
            return Response(
                {"detail": "Only tenant-owned providers can be modified by tenant admins."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ModelConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latest_version = (
            ModelConfig.objects.filter(
                provider=provider,
                model_name=serializer.validated_data["model_name"],
            )
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        model_config = serializer.save(
            provider=provider,
            version=(latest_version or 0) + 1,
        )
        return Response(
            ModelConfigSerializer(model_config).data,
            status=status.HTTP_201_CREATED,
        )


class ModelTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, model_config_id):
        tenant = get_tenant(request, tenant_id)
        if not is_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        model_config = get_object_or_404(
            ModelConfig.objects.select_related("provider").filter(
                Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
                provider__enabled=True,
                enabled=True,
            ),
            pk=model_config_id,
        )
        messages = request.data.get(
            "messages",
            [{"role": "user", "content": "Reply with a short connection test."}],
        )
        try:
            result = ModelGateway().complete(model_config, messages)
        except ModelGatewayError as exc:
            return Response(
                {
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": exc.retryable,
                    }
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result.as_dict())
