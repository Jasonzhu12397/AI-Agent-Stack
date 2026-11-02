import uuid

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_serializers import (
    AgentDraftSerializer,
    AgentRunSerializer,
    AgentVersionSerializer,
)
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    ModelConfig,
    Project,
    ProjectMembership,
    PluginInstallation,
    PluginTool,
    Task,
    Tenant,
    TenantMembership,
)


def get_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def get_project(request, tenant, project_id):
    queryset = Project.objects.filter(tenant=tenant)
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=project_id)


def can_edit_project(request, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_editor = ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()
    return tenant_admin or project_editor


def get_agent(request, agent_id):
    queryset = AgentDefinition.objects.filter(tenant__memberships__user=request.user)
    tenant_admin = TenantMembership.objects.filter(
        tenant__agents__id=agent_id,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        queryset = queryset.filter(project__memberships__user=request.user)
    return get_object_or_404(queryset.distinct(), pk=agent_id)


def can_edit_agent(request, agent):
    return can_edit_project(request, agent.tenant, agent.project)


class AgentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        agents = AgentDefinition.objects.filter(
            tenant=tenant,
            project=project,
            is_archived=False,
        ).order_by("name")
        return Response(AgentDraftSerializer(agents, many=True).data)

    def post(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        if not can_edit_project(request, tenant, project):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = AgentDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent = serializer.save(
            tenant=tenant,
            project=project,
            created_by=request.user,
        )
        return Response(
            AgentDraftSerializer(agent).data,
            status=status.HTTP_201_CREATED,
        )


class AgentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, agent_id):
        agent = get_agent(request, agent_id)
        return Response(AgentDraftSerializer(agent).data)

    def patch(self, request, agent_id):
        agent = get_agent(request, agent_id)
        if not can_edit_agent(request, agent):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if agent.is_archived:
            return Response(
                {"detail": "Archived agents are read-only."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = AgentDraftSerializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class AgentPublishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, agent_id):
        with transaction.atomic():
            agent = get_agent(request, agent_id)
            if not can_edit_agent(request, agent):
                return Response(
                    {"detail": "Project editor permission required."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            agent = AgentDefinition.objects.select_for_update().get(pk=agent.pk)
            if agent.is_archived:
                return Response(
                    {"detail": "Archived agents cannot be published."},
                    status=status.HTTP_409_CONFLICT,
                )
            config = agent.draft_config
            required = ["model_config_id", "system_prompt"]
            missing = [field for field in required if not config.get(field)]
            if missing:
                return Response(
                    {"detail": "Missing required agent configuration.", "fields": missing},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                model_config_id = uuid.UUID(str(config["model_config_id"]))
            except (TypeError, ValueError):
                return Response(
                    {"detail": "model_config_id must be a UUID."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            model_config = get_object_or_404(
                ModelConfig.objects.select_related("provider").filter(
                    enabled=True,
                    provider__enabled=True,
                ).filter(
                    models_q_for_tenant(agent.tenant)
                ),
                pk=model_config_id,
            )
            if "chat" not in model_config.capabilities:
                return Response(
                    {"detail": "Selected model does not declare chat capability."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            system_prompt = config.get("system_prompt")
            if not isinstance(system_prompt, str) or not system_prompt.strip():
                return Response(
                    {"detail": "system_prompt must be a non-empty string."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(system_prompt) > 12000:
                return Response(
                    {"detail": "system_prompt cannot exceed 12,000 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tool_allowlist = config.get("tool_allowlist", [])
            knowledge_base_ids = config.get("knowledge_base_ids", [])
            if not isinstance(tool_allowlist, list) or not isinstance(knowledge_base_ids, list):
                return Response(
                    {"detail": "tool_allowlist and knowledge_base_ids must be arrays."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if knowledge_base_ids:
                return Response(
                    {"detail": "Knowledge bases are not yet enabled for published agents."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not isinstance(tool_allowlist, list) or len(tool_allowlist) > 32:
                return Response(
                    {"detail": "tool_allowlist must contain at most 32 plugin tool IDs."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                tool_ids = [uuid.UUID(str(value)) for value in tool_allowlist]
            except (TypeError, ValueError, AttributeError):
                return Response(
                    {"detail": "tool_allowlist entries must be plugin tool UUIDs."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(set(tool_ids)) != len(tool_ids):
                return Response(
                    {"detail": "tool_allowlist cannot contain duplicate IDs."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if tool_ids:
                if "tools" not in model_config.capabilities:
                    return Response(
                        {"detail": "Selected model does not declare tools capability."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                available_tool_ids = set(
                    PluginTool.objects.filter(
                        id__in=tool_ids,
                        enabled=True,
                        plugin__tenant=agent.tenant,
                        plugin__enabled=True,
                        plugin__review_status="approved",
                        plugin__installations__tenant=agent.tenant,
                        plugin__installations__project=agent.project,
                        plugin__installations__status="installed",
                        plugin__installations__plugin_version=F("plugin__version"),
                    ).values_list("id", flat=True)
                )
                if available_tool_ids != set(tool_ids):
                    return Response(
                        {"detail": "Every allowed tool must belong to an approved connector installed in this project."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            max_iterations = config.get("max_iterations", 1)
            timeout_seconds = config.get("timeout_seconds", 60)
            max_tokens = config.get("max_tokens", 2048)
            if not isinstance(max_iterations, int) or not 1 <= max_iterations <= 8:
                return Response(
                    {"detail": "max_iterations must be between 1 and 8."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
                return Response(
                    {"detail": "timeout_seconds must be between 1 and 120."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                not isinstance(max_tokens, int)
                or not 1 <= max_tokens <= model_config.max_output_tokens
            ):
                return Response(
                    {"detail": "max_tokens exceeds the selected model output limit."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            version_number = (
                AgentVersion.objects.filter(agent=agent)
                .order_by("-version")
                .values_list("version", flat=True)
                .first()
                or 0
            ) + 1
            snapshot = {
                "model_config_id": str(model_config.id),
                "system_prompt": system_prompt,
                "tool_allowlist": [str(value) for value in tool_ids],
                "knowledge_base_ids": [],
                "max_iterations": max_iterations,
                "timeout_seconds": timeout_seconds,
                "max_tokens": max_tokens,
            }
            version = AgentVersion.objects.create(
                agent=agent,
                version=version_number,
                config_snapshot=snapshot,
                created_by=request.user,
            )
            agent.current_version = version_number
            agent.save(update_fields=["current_version", "updated_at"])
        return Response(
            AgentVersionSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )


def models_q_for_tenant(tenant):
    from django.db.models import Q

    return Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True)


class AgentRunListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, agent_id):
        agent = get_agent(request, agent_id)
        runs = (
            AgentRun.objects.filter(agent_version__agent=agent)
            .select_related("task", "agent_version")
            .order_by("-created_at")
        )
        return Response(AgentRunSerializer(runs, many=True).data)

    def post(self, request, agent_id):
        agent = get_agent(request, agent_id)
        if agent.is_archived or agent.current_version <= 0:
            return Response(
                {"detail": "Agent must have a published version before it can run."},
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
        version = get_object_or_404(
            AgentVersion,
            agent=agent,
            version=agent.current_version,
        )
        payload = {
            "agent_version_id": str(version.id),
            "input_text": input_text,
        }
        with transaction.atomic():
            task, created = Task.objects.get_or_create(
                tenant=agent.tenant,
                idempotency_key=idempotency_key,
                defaults={
                    "project": agent.project,
                    "created_by": request.user,
                    "task_type": "agent.run",
                    "payload": payload,
                    "available_at": timezone.now(),
                },
            )
            if not created and (
                task.project_id != agent.project_id
                or task.task_type != "agent.run"
                or task.payload != payload
            ):
                return Response(
                    {"detail": "Idempotency-Key was already used with a different request."},
                    status=status.HTTP_409_CONFLICT,
                )
            run, _ = AgentRun.objects.get_or_create(
                task=task,
                defaults={
                    "agent_version": version,
                    "input_text": input_text,
                },
            )
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(
            AgentRunSerializer(run).data,
            status=response_status,
        )


class AgentRunDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        run = get_object_or_404(
            AgentRun.objects.select_related(
                "task",
                "agent_version",
                "agent_version__agent",
            ).filter(
                agent_version__agent__tenant__memberships__user=request.user,
            ),
            pk=run_id,
        )
        agent = run.agent_version.agent
        tenant_admin = TenantMembership.objects.filter(
            tenant=agent.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not tenant_admin and not ProjectMembership.objects.filter(
            project=agent.project,
            user=request.user,
        ).exists():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AgentRunSerializer(run).data)
