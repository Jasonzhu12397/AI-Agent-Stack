"""Control Agent chat endpoints.

A thread belongs to one tenant project. Talking to the Control Agent means the
member can attach a project, the skills (agent capabilities) and the plugins
they want in scope; those attachments become part of the transcript record and
of the system prompt, and the answer comes from a model the tenant configured
or from the member's own linked platform account.
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import provider_accounts
from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.chat_models import AgentChatMessage, AgentChatThread
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import (
    AgentPluginInstallation,
    AuditEvent,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
)
from core.provider_accounts import AccountTokenGuard, ProviderAccountError
from core.token_budget import TenantTokenBudgetExceeded
from core.token_guard import AdHocTokenGuard

MAX_MESSAGE_CHARS = 20000
#: How many earlier turns travel to the model. The transcript keeps everything;
#: only the window is bounded, which is what a chat context budget allows.
CONTEXT_WINDOW = 20


def get_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def get_project(request, tenant, project_id):
    return get_object_or_404(Project, tenant=tenant, pk=project_id)


def thread_payload(thread, last_message=None):
    payload = {
        "id": str(thread.id),
        "title": thread.title,
        "project_id": str(thread.project_id),
        "model_config_id": str(thread.model_config_id) if thread.model_config_id else None,
        "archived": thread.archived,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
    }
    if last_message is not None:
        payload["last_message"] = {
            "role": last_message.role,
            "content": last_message.content[:240],
            "created_at": last_message.created_at.isoformat(),
        }
    return payload


def message_payload(message):
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "context": message.context or {},
        "usage": message.usage or {},
        "error_code": message.error_code,
        "created_at": message.created_at.isoformat(),
        "author": message.author.username if message.author_id else None,
    }


class AgentChatThreadListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        threads = AgentChatThread.objects.filter(
            tenant=tenant, project=project, archived=False
        ).select_related("model_config")
        items = [thread_payload(thread, thread.messages.last()) for thread in threads[:50]]
        return Response({"items": items})

    def post(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        title = request.data.get("title") or "New conversation"
        if not isinstance(title, str):
            return legacy_error_response(
                "title must be a string.",
                http_status=status.HTTP_400_BAD_REQUEST,
                code="INVALID_TITLE",
            )
        model_config = _resolve_model_config(request, tenant, request.data.get("model_config_id"))
        thread = AgentChatThread.objects.create(
            tenant=tenant,
            project=project,
            created_by=request.user,
            title=title.strip()[:200] or "New conversation",
            model_config=model_config,
        )
        return Response(thread_payload(thread), status=status.HTTP_201_CREATED)


class AgentChatThreadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _thread(self, request, thread_id):
        thread = get_object_or_404(
            AgentChatThread.objects.select_related("tenant", "project", "model_config"),
            pk=thread_id,
            tenant__memberships__user=request.user,
        )
        return thread

    def get(self, request, thread_id):
        thread = self._thread(request, thread_id)
        messages = thread.messages.select_related("author").all()
        return Response(
            {
                **thread_payload(thread),
                "messages": [message_payload(message) for message in messages],
            }
        )

    def patch(self, request, thread_id):
        thread = self._thread(request, thread_id)
        if "title" in request.data:
            title = request.data.get("title")
            if not isinstance(title, str) or not title.strip():
                return legacy_error_response(
                    "title must be a non-empty string.",
                    http_status=status.HTTP_400_BAD_REQUEST,
                    code="INVALID_TITLE",
                )
            thread.title = title.strip()[:200]
        if "archived" in request.data:
            thread.archived = bool(request.data.get("archived"))
        if "model_config_id" in request.data:
            thread.model_config = _resolve_model_config(
                request, thread.tenant, request.data.get("model_config_id")
            )
        thread.save()
        return Response(thread_payload(thread))

    def delete(self, request, thread_id):
        thread = self._thread(request, thread_id)
        thread.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AgentChatMessageListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        thread = get_object_or_404(
            AgentChatThread.objects.filter(tenant__memberships__user=request.user),
            pk=thread_id,
        )
        messages = thread.messages.select_related("author").all()
        return Response({"items": [message_payload(message) for message in messages]})

    def post(self, request, thread_id):
        thread = get_object_or_404(
            AgentChatThread.objects.select_related("tenant", "project", "model_config").filter(
                tenant__memberships__user=request.user
            ),
            pk=thread_id,
        )
        content = request.data.get("content")
        if not isinstance(content, str) or not content.strip():
            return legacy_error_response(
                "content must be a non-empty string.",
                http_status=status.HTTP_400_BAD_REQUEST,
                code="INVALID_MESSAGE",
            )
        if len(content) > MAX_MESSAGE_CHARS:
            return legacy_error_response(
                f"content must be at most {MAX_MESSAGE_CHARS} characters.",
                http_status=status.HTTP_400_BAD_REQUEST,
                code="INVALID_MESSAGE",
            )
        context = _normalize_context(thread, request.data.get("context"))
        if isinstance(context, str):
            return legacy_error_response(context, http_status=400, code="INVALID_CONTEXT")
        model_config = (
            _resolve_model_config(request, thread.tenant, request.data.get("model_config_id"))
            or thread.model_config
        )
        with transaction.atomic():
            user_message = AgentChatMessage.objects.create(
                thread=thread,
                role=AgentChatMessage.Role.USER,
                content=content.strip(),
                author=request.user,
                context=context,
            )
            thread.save(update_fields=["updated_at"])
        reply, error = _generate_reply(request, thread, model_config, context)
        if error is not None:
            return error
        with transaction.atomic():
            assistant = AgentChatMessage.objects.create(
                thread=thread,
                role=AgentChatMessage.Role.ASSISTANT,
                content=reply["content"],
                usage=reply["usage"],
            )
            thread.save(update_fields=["updated_at"])
        record_audit_event(
            request,
            thread.tenant,
            "agent.chat.message",
            "agent_chat_thread",
            thread.id,
            metadata={
                "thread_id": str(thread.id),
                "model_config_id": str(model_config.id) if model_config else None,
                "skills": context.get("skills", []),
                "plugins": context.get("plugins", []),
            },
        )
        return Response(
            {"user": message_payload(user_message), "assistant": message_payload(assistant)},
            status=status.HTTP_201_CREATED,
        )


class AgentChatContextView(APIView):
    """Everything the composer needs in one round trip."""

    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = get_tenant(request, tenant_id)
        project = get_project(request, tenant, project_id)
        installations = AgentPluginInstallation.objects.filter(
            tenant=tenant,
            project=project,
            status=AgentPluginInstallation.Status.INSTALLED,
        ).select_related("plugin")
        skills = []
        plugins = []
        seen = set()
        for installation in installations:
            plugins.append(
                {
                    "id": str(installation.id),
                    "key": installation.plugin.plugin_key,
                    "name": installation.plugin.name,
                    "version": installation.plugin_version,
                    "protocol": installation.plugin.protocol,
                }
            )
            for capability in installation.granted_capabilities or []:
                if not isinstance(capability, str) or capability in seen:
                    continue
                seen.add(capability)
                skills.append(
                    {"id": capability, "name": capability, "installation_id": str(installation.id)}
                )
        projects = Project.objects.filter(tenant=tenant).order_by("name")
        models = _available_models(request, tenant)
        return Response(
            {
                "project": {"id": str(project.id), "name": project.name, "slug": project.slug},
                "projects": [
                    {"id": str(item.id), "name": item.name, "slug": item.slug} for item in projects
                ],
                "skills": skills,
                "plugins": plugins,
                "models": models,
            }
        )


def _available_models(request, tenant):
    queryset = (
        ModelConfig.objects.select_related("provider")
        .filter(
            Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
            provider__enabled=True,
            enabled=True,
        )
        .order_by("provider__name", "model_name")
    )
    items = []
    for model in queryset:
        provider = model.provider
        account = None
        if provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT:
            link = provider_accounts.active_link(request.user, provider)
            snapshot = provider_accounts.quota_snapshot(request.user, provider) if link else None
            account = {
                "linked": link is not None,
                "remaining_today": snapshot["remaining_today"] if snapshot else 0,
            }
        items.append(
            {
                "id": str(model.id),
                "model_name": model.model_name,
                "display_name": model.display_name,
                "provider_name": provider.name,
                "auth_mode": provider.auth_mode,
                "account": account,
            }
        )
    return items


def _resolve_model_config(request, tenant, model_config_id):
    if not model_config_id:
        return None
    model_config = get_object_or_404(
        ModelConfig.objects.select_related("provider").filter(
            Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
            provider__enabled=True,
            enabled=True,
        ),
        pk=model_config_id,
    )
    return model_config


def _normalize_context(thread, raw):
    """Validate composer attachments and keep only what exists in this project."""
    context: dict = {"project_id": str(thread.project_id), "skills": [], "plugins": []}
    if raw in (None, ""):
        return context
    if not isinstance(raw, dict):
        return "context must be an object."
    project_id = raw.get("project_id")
    if project_id:
        project = (
            Project.objects.filter(tenant=thread.tenant, pk=project_id).first()
            if _is_uuid(project_id)
            else None
        )
        if project is None:
            return "context.project_id does not belong to this tenant."
        context["project_id"] = str(project.id)
    skills = raw.get("skills") or []
    if not isinstance(skills, list) or len(skills) > 100:
        return "context.skills must be a list of at most 100 ids."
    plugins = raw.get("plugins") or []
    if not isinstance(plugins, list) or len(plugins) > 50:
        return "context.plugins must be a list of at most 50 ids."
    installations = {
        str(item.id): item
        for item in AgentPluginInstallation.objects.filter(
            tenant=thread.tenant, project=thread.project
        ).select_related("plugin")
    }
    granted = set()
    for installation in installations.values():
        granted.update(
            capability
            for capability in installation.granted_capabilities or []
            if isinstance(capability, str)
        )
    for skill in skills:
        if not isinstance(skill, str) or skill not in granted:
            return f"context.skills contains an unknown skill: {skill}"
    for plugin_id in plugins:
        if not isinstance(plugin_id, str) or plugin_id not in installations:
            return "context.plugins contains a plugin not installed in this project."
    context["skills"] = sorted(set(skills))
    context["plugins"] = sorted(set(plugins))
    return context


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _system_prompt(thread, context):
    lines = [
        "You are the Control Agent of AI-Agent-Stack, a multi-agent platform.",
        "You own the delivery chain (Cloud Control, Target and EPH agents), the agent marketplace,",
        "the skill catalog and the plugin installs. Answer as that operator.",
        f"Tenant: {thread.tenant.name}",
        f"Project: {thread.project.name}",
    ]
    if context.get("skills"):
        lines.append("Skills in scope for this conversation: " + ", ".join(context["skills"]))
    plugin_names = [
        installation.plugin.plugin_key
        for installation in AgentPluginInstallation.objects.filter(
            tenant=thread.tenant, project=thread.project
        ).select_related("plugin")
        if str(installation.id) in context.get("plugins", [])
    ]
    if plugin_names:
        lines.append("Plugins in scope: " + ", ".join(plugin_names))
    lines.append("Be concrete: name the resources, the commands and the next step.")
    return "\n".join(lines)


def _history(thread):
    turns = list(thread.messages.order_by("-created_at")[:CONTEXT_WINDOW])
    turns.reverse()
    history = []
    for message in turns:
        if message.role == AgentChatMessage.Role.USER:
            history.append({"role": "user", "content": message.content})
        elif message.role == AgentChatMessage.Role.ASSISTANT and not message.error_code:
            history.append({"role": "assistant", "content": message.content})
    return history


def _generate_reply(request, thread, model_config, context):
    """Run the Control Agent turn and translate every failure into an envelope."""
    if model_config is None:
        record_audit_event(
            request,
            thread.tenant,
            "agent.chat.message",
            "agent_chat_thread",
            thread.id,
            outcome=AuditEvent.Outcome.FAILURE,
            metadata={"error_code": "MODEL_NOT_CONFIGURED"},
        )
        return None, legacy_error_response(
            "Configure a model provider for this tenant, or link your own account "
            "on a marketplace platform.",
            http_status=status.HTTP_409_CONFLICT,
            code="MODEL_NOT_CONFIGURED",
        )

    provider = model_config.provider
    messages = [{"role": "system", "content": _system_prompt(thread, context)}]
    messages.extend(_history(thread))
    user_message = thread.messages.last()
    messages.append({"role": "user", "content": user_message.content})
    idempotency_key = f"agent-chat:{thread.id}:{user_message.id}"

    try:
        if provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT:
            link = provider_accounts.active_link(request.user, provider)
            if link is None:
                return None, legacy_error_response(
                    "Link your own account on this platform before chatting through it.",
                    http_status=403,
                    code="PROVIDER_ACCOUNT_NOT_LINKED",
                )
            result = ModelGateway(account=AccountTokenGuard(link=link)).complete(
                model_config, messages, idempotency_key=idempotency_key
            )
        else:
            guard = AdHocTokenGuard(tenant=thread.tenant, scope_ref=f"agent-chat:{thread.id}")
            result = ModelGateway(guard=guard).complete(
                model_config, messages, idempotency_key=idempotency_key
            )
    except ProviderAccountError as exc:
        return None, _failure(request, thread, model_config, exc.code, str(exc), exc.http_status)
    except TenantTokenBudgetExceeded as exc:
        return None, _failure(
            request,
            thread,
            model_config,
            "TENANT_TOKEN_BUDGET_EXCEEDED",
            f"Tenant {exc.period} token budget would be exceeded.",
            status.HTTP_402_PAYMENT_REQUIRED,
        )
    except ModelGatewayError as exc:
        return None, _failure(
            request, thread, model_config, exc.code, str(exc), status.HTTP_502_BAD_GATEWAY
        )

    usage = result.as_dict().get("usage") or {}
    return {"content": result.content, "usage": usage}, None


def _failure(request, thread, model_config, code, message, http_status):
    record_audit_event(
        request,
        thread.tenant,
        "agent.chat.message",
        "agent_chat_thread",
        thread.id,
        outcome=AuditEvent.Outcome.FAILURE,
        metadata={"error_code": code, "model_config_id": str(model_config.id)},
    )
    return legacy_error_response(message, http_status=http_status, code=code)
