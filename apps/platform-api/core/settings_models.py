"""Settings-center durable storage.

Three precedence layers:

* ``TenantSetting`` -- defaults shared by every project in a tenant.
* ``ProjectSetting`` -- overrides the tenant layer inside one project.
* ``UserPreference`` -- user-local preferences (appearance, shortcuts,
  notification delivery) that override both shared layers.

``UserAccountProfile`` holds the mutable account profile fields that do not
live on the default auth user, and ``FeedbackEntry`` backs the feedback
endpoint. Both are owned by the settings-core worker alongside the settings
tables.
"""

import uuid

from django.conf import settings
from django.db import models

from core.models import Project, Tenant


class TenantSetting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    section = models.CharField(max_length=32)
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenant_settings_updated",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "section"],
                name="uniq_tenant_setting_section",
            )
        ]


class ProjectSetting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="project_settings",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    section = models.CharField(max_length=32)
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_settings_updated",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "section"],
                name="uniq_project_setting_section",
            )
        ]


class UserPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences",
    )
    namespace = models.CharField(max_length=32)
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferences_updated",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "namespace"],
                name="uniq_user_preference_namespace",
            )
        ]


class UserAccountProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="account_profile",
    )
    display_name = models.CharField(max_length=160, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    locale = models.CharField(max_length=16, default="zh-hans")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_profiles_updated",
    )


class FeedbackEntry(models.Model):
    class Category(models.TextChoices):
        BUG = "bug", "Bug"
        FEATURE = "feature", "Feature request"
        USABILITY = "usability", "Usability"
        OTHER = "other", "Other"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        related_name="feedback_entries",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="feedback_entries",
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    body = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class NotificationEndpoint(models.Model):
    """An IM group robot a tenant forwards platform notifications to.

    The webhook URL carries the bot token, so it is sealed like a credential and
    never returned by the read API. ``secret`` is the signing key DingTalk and
    Feishu group robots require; WeCom embeds it in the URL instead.
    """

    class Channel(models.TextChoices):
        DINGTALK = "dingtalk", "DingTalk"
        WECOM = "wecom", "WeCom"
        FEISHU = "feishu", "Feishu"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="notification_endpoints",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    name = models.CharField(max_length=120)
    sealed_url = models.TextField()
    sealed_secret = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("channel", "name")
        unique_together = ("tenant", "channel", "name")

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.channel}:{self.name}"
