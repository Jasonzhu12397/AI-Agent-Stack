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

from core import chat_runtime, provider_accounts
from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.chat_models import AgentChatAttachment, AgentChatMessage, AgentChatThread
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    AuditEvent,
    KnowledgeDocument,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
)

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
        "execution_target": thread.execution_target,
        "effort": thread.effort,
        "goal": thread.goal,
        "plan_first": thread.plan_first,
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
        "tool_calls": message.tool_calls or [],
        "error_code": message.error_code,
        "created_at": message.created_at.isoformat(),
        "author": message.author.username if message.author_id else None,
    }


def _apply_thread_settings(request, thread, data):
    """Validate and apply the composer settings shared by create and update."""

    if "execution_target" in data:
        target = str(data.get("execution_target") or "").strip()
        if target not in chat_runtime.EXECUTION_TARGETS:
            return legacy_error_response(
                "execution_target must be cloud or local.",
                http_status=400,
                code="INVALID_EXECUTION_TARGET",
            )
        thread.execution_target = target
    if "effort" in data:
        effort = str(data.get("effort") or "").strip()
        if effort not in chat_runtime.EFFORTS:
            return legacy_error_response(
                "effort must be auto or low.", http_status=400, code="INVALID_EFFORT"
            )
        thread.effort = effort
    if "goal" in data:
        goal = data.get("goal")
        if not isinstance(goal, str) or len(goal) > 500:
            return legacy_error_response(
                "goal must be a string of at most 500 characters.",
                http_status=400,
                code="INVALID_GOAL",
            )
        thread.goal = goal.strip()
    if "plan_first" in data:
        thread.plan_first = bool(data.get("plan_first"))
    if "model_config_id" in data:
        thread.model_config = _resolve_model_config(
            request, thread.tenant, data.get("model_config_id")
        )
    return None


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
        model_config = _resolve_model_config(
            request, tenant, request.data.get("model_config_id")
        ) or _default_model_config(tenant)
        thread = AgentChatThread.objects.create(
            tenant=tenant,
            project=project,
            created_by=request.user,
            title=title.strip()[:200] or "New conversation",
            model_config=model_config,
        )
        error = _apply_thread_settings(request, thread, request.data)
        if error is not None:
            thread.delete()
            return error
        thread.save()
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
                "attachments": [
                    attachment_payload(item) for item in thread.attachments.all()
                ],
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
        error = _apply_thread_settings(request, thread, request.data)
        if error is not None:
            return error
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
            or _default_model_config(thread.tenant)
        )
        if model_config is not None and thread.model_config_id is None:
            # Remember the model this conversation settled on so later turns
            # keep answering with the same one.
            thread.model_config = model_config
        with transaction.atomic():
            user_message = AgentChatMessage.objects.create(
                thread=thread,
                role=AgentChatMessage.Role.USER,
                content=content.strip(),
                author=request.user,
                context=context,
            )
            if thread.title in ("", "New conversation"):
                # Name the thread after its first question so the rail is
                # readable instead of a column of identical placeholders.
                thread.title = content.strip().splitlines()[0][:60]
            thread.save(update_fields=["updated_at", "model_config", "title"])
        reply, error = _generate_reply(request, thread, model_config, context)
        if error is not None:
            return error
        with transaction.atomic():
            assistant = AgentChatMessage.objects.create(
                thread=thread,
                role=AgentChatMessage.Role.ASSISTANT,
                content=reply["content"],
                usage=reply["usage"],
                tool_calls=reply.get("tool_calls") or [],
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
                    # The composer marks a plugin that still needs a credential so
                    # the member can jump straight to its authorization form.
                    "auth_required": bool(installation.plugin.auth_required),
                    "credential_configured": bool(installation.credential_ref),
                }
            )
            for capability in installation.granted_capabilities or []:
                if not isinstance(capability, str) or capability in seen:
                    continue
                seen.add(capability)
                skills.append(
                    {"id": capability, "name": capability, "installation_id": str(installation.id)}
                )
        installed_keys = {item["key"] for item in plugins}
        catalog = []
        for definition in AgentPluginDefinition.objects.filter(tenant=tenant).order_by("name"):
            if definition.plugin_key in installed_keys:
                continue
            catalog.append(
                {
                    "key": definition.plugin_key,
                    "name": definition.name,
                    "description": definition.description,
                    "protocol": definition.protocol,
                    "enabled": bool(definition.enabled),
                    "auth_required": bool(definition.auth_required),
                    "capabilities": [
                        item.get("name")
                        for item in definition.capabilities or []
                        if isinstance(item, dict) and item.get("name")
                    ],
                }
            )
        projects = Project.objects.filter(tenant=tenant).order_by("name")
        provider_accounts.sync_platform_providers(tenant=tenant, user=request.user)
        models = _available_models(request, tenant)
        return Response(
            {
                "project": {"id": str(project.id), "name": project.name, "slug": project.slug},
                "projects": [
                    {"id": str(item.id), "name": item.name, "slug": item.slug} for item in projects
                ],
                "skills": skills,
                "plugins": plugins,
                "catalog": catalog,
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
                "recommended": provider.provider_type == ModelProvider.ProviderType.FAKE,
                "model_name": model.model_name,
                "display_name": model.display_name,
                "provider_name": provider.name,
                "auth_mode": provider.auth_mode,
                "account": account,
                #: False only for a shared-pool model whose operator key is
                #: missing: the console greys it out instead of failing a send.
                "ready": (
                    provider_accounts.platform_env_configured(provider.preset_key)
                    if provider.auth_mode == ModelProvider.AuthMode.PLATFORM_POOL
                    else True
                ),
            }
        )
    return items


def _default_model_config(tenant):
    """The model a new conversation falls back to.

    A tenant that configured nothing still needs a working chat: the local
    echo provider answers without a credential, so it is preferred over a
    remote provider whose key may not be wired yet.
    """

    queryset = (
        ModelConfig.objects.select_related("provider")
        .filter(
            Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
            provider__enabled=True,
            enabled=True,
        )
        .order_by("provider__provider_type", "provider__name", "model_name")
    )
    return queryset.first()


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
    context: dict = {
        "project_id": str(thread.project_id),
        "skills": [],
        "plugins": [],
        "attachments": [],
        "knowledge_documents": [],
    }
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
    attachment_ids = raw.get("attachments") or []
    if not isinstance(attachment_ids, list) or len(attachment_ids) > 10:
        return "context.attachments must be a list of at most 10 ids."
    attachments = {
        str(item.id): item
        for item in AgentChatAttachment.objects.filter(thread=thread)
    }
    for attachment_id in attachment_ids:
        if not isinstance(attachment_id, str) or attachment_id not in attachments:
            return "context.attachments contains a file that is not attached to this thread."
    context["attachments"] = [
        attachments[attachment_id].filename for attachment_id in sorted(set(attachment_ids))
    ]
    context["attachment_ids"] = sorted(set(attachment_ids))
    document_ids = raw.get("knowledge_documents") or []
    if not isinstance(document_ids, list) or len(document_ids) > 10:
        return "context.knowledge_documents must be a list of at most 10 ids."
    documents = {
        str(item.id): item
        for item in KnowledgeDocument.objects.filter(
            knowledge_base__tenant=thread.tenant,
            knowledge_base__project=thread.project,
            knowledge_base__is_archived=False,
        )
    }
    for document_id in document_ids:
        if not isinstance(document_id, str) or document_id not in documents:
            return "context.knowledge_documents contains a document outside this project."
    context["knowledge_documents"] = [
        documents[document_id].title for document_id in sorted(set(document_ids))
    ]
    context["knowledge_document_ids"] = sorted(set(document_ids))
    return context


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _system_prompt(thread, context):
    return chat_runtime.system_prompt(thread, context)


def _history(thread):
    return chat_runtime.history(thread, CONTEXT_WINDOW)


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

    messages = [{"role": "system", "content": _system_prompt(thread, context)}]
    messages.extend(_history(thread))
    user_message = thread.messages.last()
    messages.append({"role": "user", "content": user_message.content})
    idempotency_key = f"agent-chat:{thread.id}:{user_message.id}"

    try:
        content, usage, tool_log = chat_runtime.run_turn(
            thread,
            model_config,
            user=request.user,
            messages=messages,
            context=context,
            effort=thread.effort,
            execution_target=thread.execution_target,
            idempotency_key=idempotency_key,
        )
    except chat_runtime.ChatRuntimeError as exc:
        return None, _failure(
            request, thread, model_config, exc.code, exc.message, exc.http_status
        )
    return {"content": content, "usage": usage, "tool_calls": tool_log}, None


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


#: A single attachment above this size is refused: the composer is for source
#: documents and screenshots, not for shipping datasets through a chat box.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def attachment_payload(attachment):
    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size_bytes": attachment.size_bytes,
        "created_at": attachment.created_at.isoformat(),
    }


class AgentChatAttachmentListCreateView(APIView):
    """Files the member attaches to this thread through the composer."""

    permission_classes = [IsAuthenticated]

    def _thread(self, request, thread_id):
        return get_object_or_404(
            AgentChatThread.objects.filter(tenant__memberships__user=request.user),
            pk=thread_id,
        )

    def get(self, request, thread_id):
        thread = self._thread(request, thread_id)
        return Response(
            {"items": [attachment_payload(item) for item in thread.attachments.all()]}
        )

    def post(self, request, thread_id):
        thread = self._thread(request, thread_id)
        upload = request.FILES.get("file")
        if upload is None:
            return legacy_error_response(
                "A file part named file is required.", http_status=400, code="FILE_REQUIRED"
            )
        if upload.size > MAX_ATTACHMENT_BYTES:
            return legacy_error_response(
                f"Files must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
                http_status=413,
                code="FILE_TOO_LARGE",
            )
        attachment = AgentChatAttachment.objects.create(
            thread=thread,
            uploaded_by=request.user,
            filename=upload.name[:255],
            content_type=(upload.content_type or "")[:128],
            size_bytes=upload.size,
            file=upload,
        )
        return Response(attachment_payload(attachment), status=status.HTTP_201_CREATED)


class AgentChatAttachmentDetailView(APIView):
    """Download or remove one attachment; both require thread membership."""

    permission_classes = [IsAuthenticated]

    def _attachment(self, request, attachment_id):
        return get_object_or_404(
            AgentChatAttachment.objects.select_related("thread").filter(
                thread__tenant__memberships__user=request.user
            ),
            pk=attachment_id,
        )

    def get(self, request, attachment_id):
        from django.http import FileResponse

        attachment = self._attachment(request, attachment_id)
        response = FileResponse(
            attachment.file.open("rb"),
            as_attachment=True,
            filename=attachment.filename,
            content_type=attachment.content_type or "application/octet-stream",
        )
        return response

    def delete(self, request, attachment_id):
        attachment = self._attachment(request, attachment_id)
        storage, name = attachment.file.storage, attachment.file.name
        attachment.delete()
        if name:
            storage.delete(name)
        return Response(status=status.HTTP_204_NO_CONTENT)
