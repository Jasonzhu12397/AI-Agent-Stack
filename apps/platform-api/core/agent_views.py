import uuid

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_capability_router import CapabilityResolutionError, resolve_capability
from core.agent_serializers import (
    AgentDraftSerializer,
    AgentRunSerializer,
    AgentVersionSerializer,
)
from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    KnowledgeBase,
    ModelConfig,
    PluginTool,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
)
from core.plugin_agent_binding import MARKETPLACE_AGENT_KIND


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
            return legacy_error_response(
                "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        serializer = AgentDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent = serializer.save(
            tenant=tenant,
            project=project,
            created_by=request.user,
        )
        record_audit_event(
            request,
            tenant,
            "agent.created",
            "agent_definition",
            agent.id,
            metadata={
                "slug": agent.slug,
                "name": agent.name,
                "project_id": str(project.id),
            },
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
            return legacy_error_response(
                "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
            )
        if agent.is_archived:
            return legacy_error_response(
                "Archived agents are read-only.", http_status=status.HTTP_409_CONFLICT
            )
        serializer = AgentDraftSerializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        previous_version = agent.current_version
        serializer.save()
        record_audit_event(
            request,
            agent.tenant,
            "agent.updated",
            "agent_definition",
            agent.id,
            metadata={
                "fields": sorted(serializer.validated_data.keys()),
                "previous_current_version": previous_version,
            },
        )
        return Response(serializer.data)


class AgentPublishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, agent_id):
        with transaction.atomic():
            agent = get_agent(request, agent_id)
            if not can_edit_agent(request, agent):
                return legacy_error_response(
                    "Project editor permission required.", http_status=status.HTTP_403_FORBIDDEN
                )
            agent = AgentDefinition.objects.select_for_update().get(pk=agent.pk)
            if agent.is_archived:
                return legacy_error_response(
                    "Archived agents cannot be published.", http_status=status.HTTP_409_CONFLICT
                )
            config = agent.draft_config
            if config.get("kind") == MARKETPLACE_AGENT_KIND:
                return legacy_error_response(
                    (
                        "Marketplace agents are bound from installed plugins "
                        "and cannot be republished through the agent editor."
                    ),
                    http_status=status.HTTP_409_CONFLICT,
                )
            required = ["model_config_id", "system_prompt"]
            missing = [field for field in required if not config.get(field)]
            if missing:
                return legacy_error_response(
                    "Missing required agent configuration.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                    extra={"fields": missing},
                )
            try:
                model_config_id = uuid.UUID(str(config["model_config_id"]))
            except (TypeError, ValueError):
                return legacy_error_response(
                    "model_config_id must be a UUID.", http_status=status.HTTP_400_BAD_REQUEST
                )
            model_config = get_object_or_404(
                ModelConfig.objects.select_related("provider")
                .filter(
                    enabled=True,
                    provider__enabled=True,
                )
                .filter(models_q_for_tenant(agent.tenant)),
                pk=model_config_id,
            )
            if "chat" not in model_config.capabilities:
                return legacy_error_response(
                    "Selected model does not declare chat capability.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            system_prompt = config.get("system_prompt")
            if not isinstance(system_prompt, str) or not system_prompt.strip():
                return legacy_error_response(
                    "system_prompt must be a non-empty string.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if len(system_prompt) > 12000:
                return legacy_error_response(
                    "system_prompt cannot exceed 12,000 characters.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            tool_allowlist = config.get("tool_allowlist", [])
            capability_allowlist = config.get("capability_allowlist", [])
            knowledge_base_ids = config.get("knowledge_base_ids", [])
            if (
                not isinstance(tool_allowlist, list)
                or not isinstance(capability_allowlist, list)
                or not isinstance(knowledge_base_ids, list)
            ):
                return legacy_error_response(
                    "tool_allowlist, capability_allowlist and knowledge_base_ids must be arrays.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if tool_allowlist and capability_allowlist:
                return legacy_error_response(
                    "Use either tool_allowlist or capability_allowlist, not both.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if len(knowledge_base_ids) > 8:
                return legacy_error_response(
                    "knowledge_base_ids may contain at most 8 knowledge bases.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                knowledge_ids = [uuid.UUID(str(value)) for value in knowledge_base_ids]
            except (TypeError, ValueError, AttributeError):
                return legacy_error_response(
                    "knowledge_base_ids entries must be UUIDs.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if len(set(knowledge_ids)) != len(knowledge_ids):
                return legacy_error_response(
                    "knowledge_base_ids cannot contain duplicate IDs.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if knowledge_ids:
                available_knowledge_ids = set(
                    KnowledgeBase.objects.filter(
                        id__in=knowledge_ids,
                        tenant=agent.tenant,
                        project=agent.project,
                        is_archived=False,
                        embedding_model__enabled=True,
                        embedding_model__provider__enabled=True,
                    ).values_list("id", flat=True)
                )
                if available_knowledge_ids != set(knowledge_ids):
                    return legacy_error_response(
                        ("Every knowledge base must be active and belong to this project."),
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
            if not isinstance(tool_allowlist, list) or len(tool_allowlist) > 32:
                return legacy_error_response(
                    "tool_allowlist must contain at most 32 plugin tool IDs.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                tool_ids = [uuid.UUID(str(value)) for value in tool_allowlist]
            except (TypeError, ValueError, AttributeError):
                return legacy_error_response(
                    "tool_allowlist entries must be plugin tool UUIDs.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if len(set(tool_ids)) != len(tool_ids):
                return legacy_error_response(
                    "tool_allowlist cannot contain duplicate IDs.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if tool_ids:
                if "tools" not in model_config.capabilities:
                    return legacy_error_response(
                        "Selected model does not declare tools capability.",
                        http_status=status.HTTP_400_BAD_REQUEST,
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
                    return legacy_error_response(
                        (
                            "Every allowed tool must belong to an approved connector "
                            "installed in this project."
                        ),
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
            capability_names: list[str] = []
            if capability_allowlist:
                if len(capability_allowlist) > 32:
                    return legacy_error_response(
                        "capability_allowlist may contain at most 32 capabilities.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                if not all(
                    isinstance(value, str) and value for value in capability_allowlist
                ):
                    return legacy_error_response(
                        "capability_allowlist entries must be capability names.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                if len(set(capability_allowlist)) != len(capability_allowlist):
                    return legacy_error_response(
                        "capability_allowlist cannot contain duplicate names.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                if "tools" not in model_config.capabilities:
                    return legacy_error_response(
                        "Selected model does not declare tools capability.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                    )
                for name in capability_allowlist:
                    try:
                        resolved = resolve_capability(
                            str(agent.tenant_id), str(agent.project_id), name
                        )
                    except CapabilityResolutionError as error:
                        return legacy_error_response(
                            f"{name}: {error}",
                            http_status=status.HTTP_400_BAD_REQUEST,
                            code=error.code,
                        )
                    risk = resolved.definition.get("risk_level", "low")
                    if risk in {"high", "critical"}:
                        return legacy_error_response(
                            f"{name} requires per-call approval, which published "
                            "agents do not support yet.",
                            http_status=status.HTTP_400_BAD_REQUEST,
                        )
                    capability_names.append(name)
            max_iterations = config.get("max_iterations", 4)
            timeout_seconds = config.get("timeout_seconds", 60)
            max_tokens = config.get("max_tokens", 2048)
            if (
                not isinstance(max_iterations, int)
                or isinstance(max_iterations, bool)
                or not 1 <= max_iterations <= 8
            ):
                return legacy_error_response(
                    "max_iterations must be between 1 and 8.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or not 1 <= timeout_seconds <= 120
            ):
                return legacy_error_response(
                    "timeout_seconds must be between 1 and 120.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                not isinstance(max_tokens, int)
                or isinstance(max_tokens, bool)
                or not 1 <= max_tokens <= model_config.max_output_tokens
            ):
                return legacy_error_response(
                    "max_tokens exceeds the selected model output limit.",
                    http_status=status.HTTP_400_BAD_REQUEST,
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
                "capability_allowlist": capability_names,
                "knowledge_base_ids": [str(value) for value in knowledge_ids],
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
            record_audit_event(
                request,
                agent.tenant,
                "agent.published",
                "agent_version",
                version.id,
                metadata={
                    "agent_id": str(agent.id),
                    "version": version_number,
                    "model_config_id": snapshot["model_config_id"],
                    "tool_count": len(snapshot["tool_allowlist"])
                    + len(snapshot["capability_allowlist"]),
                    "knowledge_base_count": len(snapshot["knowledge_base_ids"]),
                },
            )
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
            return legacy_error_response(
                "Agent must have a published version before it can run.",
                http_status=status.HTTP_409_CONFLICT,
            )
        input_text = request.data.get("input")
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 20000:
            return legacy_error_response(
                "input must be a non-empty string no longer than 20,000 characters.",
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        if not idempotency_key or len(idempotency_key) > 128:
            return legacy_error_response(
                "A valid Idempotency-Key header is required.",
                http_status=status.HTTP_400_BAD_REQUEST,
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
                return legacy_error_response(
                    "Idempotency-Key was already used with a different request.",
                    http_status=status.HTTP_409_CONFLICT,
                )
            run, _ = AgentRun.objects.get_or_create(
                task=task,
                defaults={
                    "agent_version": version,
                    "input_text": input_text,
                },
            )
        record_audit_event(
            request,
            agent.tenant,
            "agent.run.created",
            "agent_run",
            run.id,
            metadata={
                "agent_id": str(agent.id),
                "task_id": str(task.id),
                "agent_version_id": str(version.id),
                "deduplicated": not created,
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
        if (
            not tenant_admin
            and not ProjectMembership.objects.filter(
                project=agent.project,
                user=request.user,
            ).exists()
        ):
            return legacy_error_response("Not found.", http_status=status.HTTP_404_NOT_FOUND)
        return Response(AgentRunSerializer(run).data)
