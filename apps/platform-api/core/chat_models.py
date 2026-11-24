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
