from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

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


class TenantMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)
        memberships = TenantMembership.objects.filter(tenant=tenant).select_related("user").order_by("user__username")
        return Response([
            {"id": str(item.id), "user_id": item.user_id, "username": item.user.get_username(), "email": item.user.email, "role": item.role}
            for item in memberships
        ])

    def post(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)
        username = request.data.get("username")
        role = request.data.get("role", TenantMembership.Role.MEMBER)
        valid_roles = {choice for choice, _ in TenantMembership.Role.choices}
        if not username or role not in valid_roles:
            return Response({"detail": "A valid username and role are required."}, status=400)
        if role == TenantMembership.Role.OWNER and not require_tenant_owner(request, tenant):
            return Response({"detail": "Only an owner can grant the owner role."}, status=403)
        user = get_object_or_404(get_user_model(), username=username)
        membership, created = TenantMembership.objects.get_or_create(
            tenant=tenant,
            user=user,
            defaults={"role": role},
        )
        if not created:
            return Response({"detail": "User is already a tenant member."}, status=409)
        return Response(
            {"id": str(membership.id), "username": user.get_username(), "role": membership.role},
            status=status.HTTP_201_CREATED,
        )


class TenantMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, tenant_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)
        role = request.data.get("role")
        valid_roles = {choice for choice, _ in TenantMembership.Role.choices}
        if role not in valid_roles:
            return Response({"detail": "A valid role is required."}, status=400)
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)
            membership = get_object_or_404(
                TenantMembership.objects.select_for_update().select_related("user"),
                tenant=tenant,
                pk=membership_id,
            )
            actor_is_owner = require_tenant_owner(request, tenant)
            if membership.role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response({"detail": "Only an owner can change an owner role."}, status=403)
            if role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response({"detail": "Only an owner can grant the owner role."}, status=403)
            if membership.role == TenantMembership.Role.OWNER and role != TenantMembership.Role.OWNER:
                owner_count = TenantMembership.objects.filter(
                    tenant=tenant,
                    role=TenantMembership.Role.OWNER,
                ).count()
                if owner_count <= 1:
                    return Response({"detail": "A tenant must retain at least one owner."}, status=409)
            membership.role = role
            membership.save(update_fields=["role"])
        return Response({"id": str(membership.id), "username": membership.user.get_username(), "role": membership.role})

    def delete(self, request, tenant_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        if not require_tenant_admin(request, tenant):
            return Response({"detail": "Tenant administrator permission required."}, status=403)
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)
            membership = get_object_or_404(
                TenantMembership.objects.select_for_update().select_related("user"),
                tenant=tenant,
                pk=membership_id,
            )
            actor_is_owner = require_tenant_owner(request, tenant)
            if membership.role == TenantMembership.Role.OWNER and not actor_is_owner:
                return Response({"detail": "Only an owner can remove an owner."}, status=403)
            if membership.role == TenantMembership.Role.OWNER:
                owner_count = TenantMembership.objects.filter(
                    tenant=tenant,
                    role=TenantMembership.Role.OWNER,
                ).count()
                if owner_count <= 1:
                    return Response({"detail": "A tenant must retain at least one owner."}, status=409)
            ProjectMembership.objects.filter(
                project__tenant=tenant,
                user=membership.user,
            ).delete()
            membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response({"detail": "Project administrator permission required."}, status=403)
        memberships = ProjectMembership.objects.filter(project=project).select_related("user").order_by("user__username")
        return Response([
            {"id": str(item.id), "user_id": item.user_id, "username": item.user.get_username(), "email": item.user.email, "role": item.role}
            for item in memberships
        ])

    def post(self, request, tenant_id, project_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response({"detail": "Project administrator permission required."}, status=403)
        username = request.data.get("username")
        role = request.data.get("role", ProjectMembership.Role.VIEWER)
        valid_roles = {choice for choice, _ in ProjectMembership.Role.choices}
        if not username or role not in valid_roles:
            return Response({"detail": "A valid username and role are required."}, status=400)
        user = get_object_or_404(get_user_model(), username=username)
        if not TenantMembership.objects.filter(tenant=tenant, user=user).exists():
            return Response({"detail": "User must belong to the tenant before joining a project."}, status=409)
        membership, created = ProjectMembership.objects.get_or_create(
            project=project,
            user=user,
            defaults={"role": role},
        )
        if not created:
            return Response({"detail": "User is already a project member."}, status=409)
        return Response(
            {"id": str(membership.id), "username": user.get_username(), "role": membership.role},
            status=status.HTTP_201_CREATED,
        )


class ProjectMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, tenant_id, project_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response({"detail": "Project administrator permission required."}, status=403)
        role = request.data.get("role")
        valid_roles = {choice for choice, _ in ProjectMembership.Role.choices}
        if role not in valid_roles:
            return Response({"detail": "A valid role is required."}, status=400)
        membership = get_object_or_404(ProjectMembership, project=project, pk=membership_id)
        membership.role = role
        membership.save(update_fields=["role"])
        return Response({"id": str(membership.id), "username": membership.user.get_username(), "role": membership.role})

    def delete(self, request, tenant_id, project_id, membership_id):
        tenant = tenant_for_user(request, tenant_id)
        project = project_for_user(request, tenant, project_id)
        if not require_project_admin(request, tenant, project):
            return Response({"detail": "Project administrator permission required."}, status=403)
        membership = get_object_or_404(ProjectMembership, project=project, pk=membership_id)
        if membership.role == ProjectMembership.Role.ADMIN:
            admin_count = ProjectMembership.objects.filter(
                project=project,
                role=ProjectMembership.Role.ADMIN,
            ).count()
            if admin_count <= 1:
                return Response({"detail": "A project must retain at least one administrator."}, status=409)
        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
