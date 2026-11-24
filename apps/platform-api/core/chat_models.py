"""Conversation records for the Control Agent chat surface.

The console gives every tenant project a chat where a member talks to the
Control Agent: the agent that owns the delivery chain and can reach the
marketplace, the skill catalog and the plugin installs of that project. A
thread is the durable transcript; a message is one turn plus the context the
member attached to it (project, skills, plugins) and the usage the model
reported, so the reply can be audited after the fact.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from core.models import ModelConfig, Project, Tenant


class AgentChatThread(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_chat_threads")
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="agent_chat_threads"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="agent_chat_threads",
    )
    title = models.CharField(max_length=200, default="New conversation")
    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_chat_threads",
    )
    archived = models.BooleanField(default=False)
    # The composer's durable settings: where the work runs, what the member is
    # trying to achieve, whether the answer should lead with a plan, and how
    # much budget a single turn may spend.
    execution_target = models.CharField(
        max_length=16,
        choices=[("cloud", "Cloud"), ("local", "Local")],
        default="cloud",
    )
    effort = models.CharField(
        max_length=16,
        choices=[("auto", "Auto"), ("low", "Low")],
        default="auto",
    )
    goal = models.CharField(max_length=500, blank=True)
    plan_first = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "project", "-updated_at"], name="chat_thread_scope_idx")
        ]

    def __str__(self) -> str:
        return f"{self.project}/{self.title}"


class AgentChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(AgentChatThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_chat_messages",
    )
    # Project, skills and plugins the member attached through the composer.
    context = models.JSONField(default=dict, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["thread", "created_at"], name="chat_message_thread_idx")]

    def __str__(self) -> str:
        return f"{self.thread_id}:{self.role}"


class AgentChatAttachment(models.Model):
    """A file the member attached to a thread through the composer.

    The bytes live on the platform's media volume; the message records only the
    attachment ids, so a transcript stays readable after the file is deleted and
    the prompt can name exactly what the model was shown.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        AgentChatThread, on_delete=models.CASCADE, related_name="attachments"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="agent_chat_attachments",
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=128, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    file = models.FileField(upload_to="chat-attachments/%Y/%m/")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["thread", "created_at"], name="chat_attachment_thread_idx")
        ]

    def __str__(self) -> str:
        return f"{self.thread_id}/{self.filename}"
