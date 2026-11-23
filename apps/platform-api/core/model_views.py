import uuid

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.model_gateway import ModelGateway, ModelGatewayError
from core.model_serializers import (
    ModelConfigSerializer,
    ModelProviderReadSerializer,
    ModelProviderWriteSerializer,
)
from core.models import (
    AuditEvent,
    ModelConfig,
    ModelProvider,
    Tenant,
    TenantMembership,
)
from core.token_budget import TenantTokenBudgetExceeded
from core.token_guard import AdHocTokenGuard


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
        if ModelProvider.objects.filter(
            tenant=tenant,
            name=serializer.validated_data["name"],
        ).exists():
            return Response(
                {"detail": "A model provider with this name already exists."},
                status=status.HTTP_409_CONFLICT,
            )
        provider = serializer.save(tenant=tenant, created_by=request.user)
        record_audit_event(
            request,
            tenant,
            "model.provider.created",
            "model_provider",
            provider.id,
            metadata={"name": provider.name},
        )
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
        with transaction.atomic():
            provider = self.get_provider(request, tenant, provider_id)
            if provider.tenant_id != tenant.id:
                return Response(
                    {"detail": "Only tenant-owned providers can be modified by tenant admins."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            provider = ModelProvider.objects.select_for_update().get(pk=provider.pk)
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
        record_audit_event(
            request,
            tenant,
            "model.config.created",
            "model_config",
            model_config.id,
            metadata={
                "provider_id": str(provider.id),
                "model_name": model_config.model_name,
                "version": model_config.version,
            },
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
        request_id = str(request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:160]
        guard = AdHocTokenGuard(tenant=tenant, scope_ref=f"model-test:{model_config.id}")
        try:
            result = ModelGateway(guard=guard).complete(
                model_config,
                messages,
                idempotency_key=f"model-test:{request_id}",
            )
        except TenantTokenBudgetExceeded as exc:
            record_audit_event(
                request,
                tenant,
                "model.config.tested",
                "model_config",
                model_config.id,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"error_code": "TENANT_TOKEN_BUDGET_EXCEEDED", "period": exc.period},
            )
            return Response(
                {
                    "error": {
                        "code": "TENANT_TOKEN_BUDGET_EXCEEDED",
                        "message": f"Tenant {exc.period} token budget would be exceeded.",
                        "details": {"period": exc.period},
                    }
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except ModelGatewayError as exc:
            record_audit_event(
                request,
                tenant,
                "model.config.tested",
                "model_config",
                model_config.id,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"error_code": exc.code, "retryable": exc.retryable},
            )
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
        record_audit_event(
            request,
            tenant,
            "model.config.tested",
            "model_config",
            model_config.id,
            metadata={
                "provider_id": str(model_config.provider_id),
                "model_name": model_config.model_name,
            },
        )
        return Response(result.as_dict())
