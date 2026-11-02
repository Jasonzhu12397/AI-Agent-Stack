from rest_framework.permissions import BasePermission

from core.models import TenantMembership


class IsTenantAdmin(BasePermission):
    message = "Tenant owner or admin role is required."

    def has_permission(self, request, view):
        tenant = view.get_tenant()
        return TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
