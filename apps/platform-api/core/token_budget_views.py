from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.models import Tenant, TenantTokenBudget
from core.permissions import IsTenantAdmin
from core.token_budget import get_tenant_token_usage


class StrictPositiveIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if not isinstance(data, int) or isinstance(data, bool):
            raise serializers.ValidationError("A positive integer is required.")
        return super().to_internal_value(data)


class TenantTokenBudgetUpdateSerializer(serializers.Serializer):
    daily_limit = StrictPositiveIntegerField(min_value=1, max_value=2**63 - 1)
    monthly_limit = StrictPositiveIntegerField(min_value=1, max_value=2**63 - 1)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one budget limit must be supplied.")
        return attrs


class TenantTokenBudgetView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        permissions = [IsAuthenticated]
        if self.request.method in {"PUT", "PATCH"}:
            permissions.append(IsTenantAdmin)
        return [permission() for permission in permissions]

    def get_tenant(self):
        return get_object_or_404(
            Tenant.objects.filter(memberships__user=self.request.user),
            pk=self.kwargs["tenant_id"],
        )

    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        budget, _ = TenantTokenBudget.objects.get_or_create(tenant=tenant)
        now = timezone.now()
        return Response(
            {
                "daily_limit": budget.daily_limit,
                "monthly_limit": budget.monthly_limit,
                **get_tenant_token_usage(tenant.id, now),
            }
        )

    @transaction.atomic
    def put(self, request, tenant_id):
        return self._update(request, tenant_id)

    @transaction.atomic
    def patch(self, request, tenant_id):
        return self._update(request, tenant_id)

    def _update(self, request, tenant_id):
        tenant = self.get_tenant()
        serializer = TenantTokenBudgetUpdateSerializer(
            data=request.data,
            partial=request.method == "PATCH",
        )
        serializer.is_valid(raise_exception=True)
        budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(
            tenant=tenant
        )
        for field, value in serializer.validated_data.items():
            setattr(budget, field, value)
        budget.save(update_fields=[*serializer.validated_data, "updated_at"])
        record_audit_event(
            request,
            tenant,
            "token_budget.updated",
            "tenant_token_budget",
            budget.id,
            metadata={field: value for field, value in serializer.validated_data.items()},
        )
        now = timezone.now()
        return Response(
            {
                "daily_limit": budget.daily_limit,
                "monthly_limit": budget.monthly_limit,
                **get_tenant_token_usage(tenant.id, now),
            },
            status=status.HTTP_200_OK,
        )
