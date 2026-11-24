"""Durable session metadata.

``django_session`` only stores the encoded session payload and its expiry, so a
session list cannot honestly report when a session was created or last used.
This module adds a small side table keyed by the session key plus the two hooks
that keep it current:

* ``user_logged_in`` records the creation time of a fresh session;
* ``SessionActivityMiddleware`` refreshes ``last_activity`` at most once per
  interval, so every authenticated request costs one conditional UPDATE instead
  of a row read.

Rows are metadata only: no IP address and no user agent are stored, and
revoked rows are kept (with ``revoked_at``) so an audit trail outlives the
session itself.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db import models, transaction
from django.dispatch import receiver
from django.utils import timezone

# How often an authenticated request may move ``last_activity`` forward.
ACTIVITY_REFRESH_INTERVAL = timedelta(seconds=60)


class SessionActivity(models.Model):
    """One row per issued Django session key."""

    session_key = models.CharField(max_length=64, primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="session_activities",
    )
    created_at = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(default=timezone.now, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_activity"]
        verbose_name_plural = "session activities"

    def __str__(self) -> str:
        return f"{self.session_key[:8]}… ({self.user_id})"


def record_login(*, session_key: str, user) -> SessionActivity | None:
    """Create the metadata row for a session key, idempotently."""

    if not session_key:
        return None
    now = timezone.now()
    activity, created = SessionActivity.objects.get_or_create(
        session_key=session_key,
        defaults={"user": user, "created_at": now, "last_activity": now},
    )
    if not created:
        # A reused key (e.g. a rotated cycle that keeps the key) refreshes the
        # owner instead of resurrecting stale metadata.
        SessionActivity.objects.filter(pk=session_key).update(
            user=user, last_activity=now, revoked_at=None
        )
    return activity


def touch(*, session_key: str, user) -> None:
    """Advance ``last_activity`` only when it is stale.

    A single conditional UPDATE keeps this off the request hot path: no row is
    fetched, and a burst of requests in the same interval writes once.
    """

    if not session_key:
        return
    now = timezone.now()
    cutoff = now - ACTIVITY_REFRESH_INTERVAL
    updated = SessionActivity.objects.filter(
        pk=session_key, last_activity__lt=cutoff
    ).update(last_activity=now)
    if updated:
        return
    if SessionActivity.objects.filter(pk=session_key).exists():
        return
    with transaction.atomic():
        SessionActivity.objects.get_or_create(
            session_key=session_key,
            defaults={"user": user, "created_at": now, "last_activity": now},
        )


def mark_revoked(session_key: str) -> None:
    if not session_key:
        return
    SessionActivity.objects.filter(pk=session_key, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def describe(session_key: str) -> dict:
    """Metadata for one key, or an empty dict when nothing is recorded."""

    row = SessionActivity.objects.filter(pk=session_key).first()
    if row is None:
        return {}
    return {
        "created_at": row.created_at,
        "last_activity": row.last_activity,
        "revoked_at": row.revoked_at,
    }


class SessionActivityMiddleware:
    """Refresh session metadata for authenticated requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        if user is not None and user.is_authenticated and session is not None:
            try:
                touch(session_key=session.session_key or "", user=user)
            except Exception:  # pragma: no cover - never break a request over metadata
                pass
        return self.get_response(request)


@receiver(user_logged_in, dispatch_uid="core.session_activity.login")
def _on_login(sender, request, user, **kwargs):
    session = getattr(request, "session", None) if request is not None else None
    if session is None:
        return
    record_login(session_key=session.session_key or "", user=user)


@receiver(user_logged_out, dispatch_uid="core.session_activity.logout")
def _on_logout(sender, request, user, **kwargs):
    session = getattr(request, "session", None) if request is not None else None
    if session is None:
        return
    mark_revoked(session.session_key or "")
