from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.models import Project, ProjectMembership, Tenant, TenantMembership


def tenant_for_user(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def require_tenant_admin(request, tenant):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()


def require_tenant_owner(request, tenant):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role=TenantMembership.Role.OWNER,
    ).exists()


def project_for_user(request, tenant, project_id):
    queryset = Project.objects.filter(tenant=tenant)
    if not require_tenant_admin(request, tenant):
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


def require_project_admin(request, tenant, project):
    return require_tenant_admin(request, tenant) or ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role=ProjectMembership.Role.ADMIN,
    ).exists()


def membership_payload(membership):
    return {
        "id": str(membership.id),
        "user_id": membership.user_id,
        "username": membership.user.get_username(),
        "email": membership.user.email,
        "role": membership.role,
    }


class TenantMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        memberships = (
            TenantMembership.objects.filter(tenant=tenant)
            .select_related("user")
            .order_by("user__username")
        )
        return Response([membership_payload(item) for item in memberships])

    def post(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        username = request.data.get("username")
        role = request.data.get("role", TenantMembership.Role.MEMBER)
        valid_roles = {choice for choice, _ in TenantMembership.Role.choices}
        if not username or role not in valid_roles:
            return Response(
                {"detail": "A valid username and role are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if role == TenantMembership.Role.OWNER and not require_tenant_owner(request, tenant):
            return Response(
                {"detail": "Only an owner can grant the owner role."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user = get_object_or_404(get_user_model(), username=username)
        membership, created = TenantMembership.objects.get_or_create(
            tenant=tenant,
            user=user,
            defaults={"role": role},
        )
        if not created:
            return Response(
                {"detail": "User is already a tenant member."},
                status=status.HTTP_409_CONFLICT,
            )
        record_audit_event(
            request,
            tenant,
            "tenant.member.added",
            "tenant_membership",
            membership.id,
            metadata={"username": user.get_username(), "role": membership.role},
        )
        return Response(
            membership_payload(membership),
            status=status.HTTP_201_CREATED,
        )


class TenantMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, tenant_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        role = request.data.get("role")
        valid_roles = {choice for choice, _ in TenantMembership.Role.choices}
        if role not in valid_roles:
            return Response(
                {"detail": "A valid role is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)
            membership = get_object_or_404(
                TenantMembership.objects.select_for_update().select_related("user"),
                tenant=tenant,
                pk=membership_id,
            )
            actor_is_owner = require_tenant_owner(request, tenant)
            if membership.role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response(
                    {"detail": "Only an owner can change an owner role."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response(
                    {"detail": "Only an owner can grant the owner role."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if (
                membership.role == TenantMembership.Role.OWNER
                and role != TenantMembership.Role.OWNER
            ):
                owner_count = TenantMembership.objects.filter(
                    tenant=tenant,
                    role=TenantMembership.Role.OWNER,
                ).count()
                if owner_count <= 1:
                    return Response(
                        {"detail": "A tenant must retain at least one owner."},
                        status=status.HTTP_409_CONFLICT,
                    )
            old_role = membership.role
            membership.role = role
            membership.save(update_fields=["role"])
        record_audit_event(
            request,
            tenant,
            "tenant.member.role_changed",
            "tenant_membership",
            membership.id,
            metadata={"username": membership.user.get_username(), "old_role": old_role, "new_role": role},
        )
        return Response(membership_payload(membership))

    def delete(self, request, tenant_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)
            membership = get_object_or_404(
                TenantMembership.objects.select_for_update().select_related("user"),
                tenant=tenant,
                pk=membership_id,
            )
            actor_is_owner = require_tenant_owner(request, tenant)
            if membership.role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response(
                    {"detail": "Only an owner can remove an owner."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if membership.role == TenantMembership.Role.OWNER:
                owner_count = TenantMembership.objects.filter(
                    tenant=tenant,
                    role=TenantMembership.Role.OWNER,
                ).count()
                if owner_count <= 1:
                    return Response(
                        {"detail": "A tenant must retain at least one owner."},
                        status=status.HTTP_409_CONFLICT,
                    )
            ProjectMembership.objects.filter(
                project__tenant=tenant,
                user=membership.user,
            ).delete()
            removed_username = membership.user.get_username()
            removed_role = membership.role
            removed_membership_id = membership.id
            membership.delete()
        record_audit_event(
            request,
            tenant,
            "tenant.member.removed",
            "tenant_membership",
            removed_membership_id,
            metadata={"username": removed_username, "role": removed_role},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        memberships = (
            ProjectMembership.objects.filter(project=project)
            .select_related("user")
            .order_by("user__username")
        )
        return Response([membership_payload(item) for item in memberships])

    def post(self, request, tenant_id, project_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        username = request.data.get("username")
        role = request.data.get("role", ProjectMembership.Role.VIEWER)
        valid_roles = {choice for choice, _ in ProjectMembership.Role.choices}
        if not username or role not in valid_roles:
            return Response(
                {"detail": "A valid username and role are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = get_object_or_404(get_user_model(), username=username)
        if not TenantMembership.objects.filter(tenant=tenant, user=user).exists():
            return Response(
                {"detail": "User must belong to the tenant before joining a project."},
                status=status.HTTP_409_CONFLICT,
            )
        membership, created = ProjectMembership.objects.get_or_create(
            project=project,
            user=user,
            defaults={"role": role},
        )
        if not created:
            return Response(
                {"detail": "User is already a project member."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            membership_payload(membership),
            status=status.HTTP_201_CREATED,
        )


class ProjectMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, tenant_id, project_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        role = request.data.get("role")
        valid_roles = {choice for choice, _ in ProjectMembership.Role.choices}
        if role not in valid_roles:
            return Response(
                {"detail": "A valid role is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project.pk)
            membership = get_object_or_404(
                ProjectMembership.objects.select_for_update().select_related("user"),
                project=project,
                pk=membership_id,
            )
            if (
                membership.role == ProjectMembership.Role.ADMIN
                and role != ProjectMembership.Role.ADMIN
            ):
                admin_count = ProjectMembership.objects.filter(
                    project=project,
                    role=ProjectMembership.Role.ADMIN,
                ).count()
                if admin_count <= 1:
                    return Response(
                        {"detail": "A project must retain at least one administrator."},
                        status=status.HTTP_409_CONFLICT,
                    )
            membership.role = role
            membership.save(update_fields=["role"])
        return Response(membership_payload(membership))

    def delete(self, request, tenant_id, project_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response(
                {"detail": "Project administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project.pk)
            membership = get_object_or_404(
                ProjectMembership.objects.select_for_update(),
                project=project,
                pk=membership_id,
            )
            if membership.role == ProjectMembership.Role.ADMIN:
                admin_count = ProjectMembership.objects.filter(
                    project=project,
                    role=ProjectMembership.Role.ADMIN,
                ).count()
                if admin_count <= 1:
                    return Response(
                        {"detail": "A project must retain at least one administrator."},
                        status=status.HTTP_409_CONFLICT,
                    )
            membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
