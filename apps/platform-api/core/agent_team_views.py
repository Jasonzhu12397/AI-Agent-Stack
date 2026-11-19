from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_serializers import (
    AgentTeamDraftSerializer,
    AgentTeamRunSerializer,
    AgentTeamVersionSerializer,
)
from core.agent_team_service import AgentTeamConfigurationError, publish_team
from core.agent_views import (
    can_edit_project,
    get_project,
    get_tenant,
)
from core.audit import record_audit_event
from core.models import (
    AgentTeam,
    AgentTeamRun,
    AgentTeamVersion,
    ProjectMembership,
    Task,
    TenantMembership,
    WorkflowRun,
)


def get_team(request, team_id):
    queryset = AgentTeam.objects.filter(tenant__memberships__user=request.user)
    tenant_admin = TenantMembership.objects.filter(
        tenant__agent_teams__id=team_id,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(project__memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=team_id)


def can_edit_team(request, team):
    return can_edit_project(request, team.tenant, team.project)


class AgentTeamListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        teams = AgentTeam.objects.filter(
            tenant=tenant,
            project=project,
            is_archived=False,
        ).order_by("name")
        return Response(AgentTeamDraftSerializer(teams, many=True).data)

    def post(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        if not can_edit_project(request, tenant, project):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = AgentTeamDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        team = serializer.save(
            tenant=tenant,
            project=project,
            created_by=request.user,
        )
        record_audit_event(
            request,
            tenant,
            "agent_team.created",
            "agent_team",
            team.id,
            metadata={"slug": team.slug, "name": team.name, "project_id": str(project.id)},
        )
        return Response(
            AgentTeamDraftSerializer(team).data,
            status=status.HTTP_201_CREATED,
        )


class AgentTeamDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        team = get_team(request, team_id)
        return Response(AgentTeamDraftSerializer(team).data)

    def patch(self, request, team_id):
        team = get_team(request, team_id)
        if not can_edit_team(request, team):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if team.is_archived:
            return Response(
                {"detail": "Archived teams are read-only."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = AgentTeamDraftSerializer(team, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_audit_event(
            request,
            team.tenant,
            "agent_team.updated",
            "agent_team",
            team.id,
            metadata={"fields": sorted(serializer.validated_data.keys())},
        )
        return Response(serializer.data)


class AgentTeamPublishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, team_id):
        team = get_team(request, team_id)
        if not can_edit_team(request, team):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if team.is_archived:
            return Response(
                {"detail": "Archived teams cannot be published."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            version = publish_team(team, request.user)
        except AgentTeamConfigurationError as exc:
            return Response(
                {"code": exc.code, "detail": exc.detail, "fields": exc.fields},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record_audit_event(
            request,
            team.tenant,
            "agent_team.published",
            "agent_team_version",
            version.id,
            metadata={
                "team_id": str(team.id),
                "version": version.version,
                "workflow_version_id": str(version.workflow_version_id),
                "strategy": version.definition_snapshot["strategy"],
                "member_count": len(version.definition_snapshot["members"]),
            },
        )
        return Response(
            AgentTeamVersionSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )


class AgentTeamRunListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        team = get_team(request, team_id)
        runs = (
            AgentTeamRun.objects.filter(team_version__team=team)
            .select_related("workflow_run", "workflow_run__task")
            .order_by("-created_at")
        )
        return Response(AgentTeamRunSerializer(runs, many=True).data)

    def post(self, request, team_id):
        team = get_team(request, team_id)
        if team.is_archived or team.current_version <= 0:
            return Response(
                {"detail": "Team must have a published version before it can run."},
                status=status.HTTP_409_CONFLICT,
            )
        input_text = request.data.get("input")
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 20000:
            return Response(
                {"detail": "input must be a non-empty string no longer than 20,000 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not idempotency_key or len(idempotency_key) > 128:
            return Response(
                {"detail": "A valid Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        team_version = get_object_or_404(
            AgentTeamVersion,
            team=team,
            version=team.current_version,
        )
        payload = {
            "team_version_id": str(team_version.id),
            "input_text": input_text,
        }
        with transaction.atomic():
            task, created = Task.objects.get_or_create(
                tenant=team.tenant,
                idempotency_key=idempotency_key,
                defaults={
                    "project": team.project,
                    "created_by": request.user,
                    "task_type": "team.run",
                    "payload": payload,
                    "available_at": timezone.now(),
                },
            )
            if not created and (
                task.project_id != team.project_id
                or task.task_type != "team.run"
                or task.payload != payload
            ):
                return Response(
                    {"detail": "Idempotency-Key was already used with a different request."},
                    status=status.HTTP_409_CONFLICT,
                )
            workflow_run, _ = WorkflowRun.objects.get_or_create(
                task=task,
                defaults={
                    "workflow_version": team_version.workflow_version,
                    "input_data": {"input": input_text},
                },
            )
            run, _ = AgentTeamRun.objects.get_or_create(
                workflow_run=workflow_run,
                defaults={
                    "team_version": team_version,
                    "input_text": input_text,
                },
            )
        record_audit_event(
            request,
            team.tenant,
            "agent_team.run.created",
            "agent_team_run",
            run.id,
            metadata={
                "team_id": str(team.id),
                "task_id": str(task.id),
                "team_version_id": str(team_version.id),
                "deduplicated": not created,
            },
        )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(AgentTeamRunSerializer(run).data, status=response_status)


class AgentTeamRunDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        run = get_object_or_404(
            AgentTeamRun.objects.select_related(
                "team_version",
                "team_version__team",
                "workflow_run",
                "workflow_run__task",
            ).filter(
                team_version__team__tenant__memberships__user=request.user,
            ),
            pk=run_id,
        )
        team = run.team_version.team
        tenant_admin = TenantMembership.objects.filter(
            tenant=team.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not tenant_admin and not ProjectMembership.objects.filter(
            project=team.project,
            user=request.user,
        ).exists():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AgentTeamRunSerializer(run).data)
