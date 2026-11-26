"""Project-scoped realtime notifications for task and audit events.

PostgreSQL is the source of truth and, when available, also the transport:
committed writes call ``pg_notify`` and an SSE connection holds a dedicated
LISTEN connection. SQLite has no LISTEN/NOTIFY, so the same SSE endpoint falls
back to a bounded query poll. That fallback is only for local development and
tests; production uses PostgreSQL notifications and never polls while idle.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.utils import timezone

CHANNEL = "platform_events"
MAX_NOTIFICATION_BYTES = 7000
DEFAULT_POLL_SECONDS = 2.0
MAX_STREAM_SECONDS = 3600


def _notification_payload(tenant_id, project_id, kind, resource_id):
    payload = {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "project_id": str(project_id) if project_id else None,
        "kind": kind,
        "id": str(resource_id) if resource_id else None,
        "at": timezone.now().isoformat(),
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_NOTIFICATION_BYTES:
        return None
    return encoded


def _send_notification(payload):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, payload])
    except Exception:
        # A notification is an optimization. The SSE consumer re-queries the
        # database after every wakeup, so a failed NOTIFY must not fail the
        # business write that triggered it.
        return


def publish_scope_event(tenant_id, project_id, kind, resource_id):
    if not tenant_id or not project_id:
        return
    if connection.vendor != "postgresql":
        return
    payload = _notification_payload(tenant_id, project_id, kind, resource_id)
    if payload is None:
        return
    transaction.on_commit(lambda: _send_notification(payload))


def _notify_task(sender, instance, created, **kwargs):
    publish_scope_event(
        instance.tenant_id,
        instance.project_id,
        "task.created" if created else "task.updated",
        instance.id,
    )


def _notify_audit(sender, instance, created, **kwargs):
    publish_scope_event(instance.tenant_id, instance.project_id, "audit.created", instance.id)


def connect():
    from core.models import AuditEvent, Task

    post_save.connect(_notify_task, sender=Task, dispatch_uid="realtime.task")
    post_save.connect(_notify_audit, sender=AuditEvent, dispatch_uid="realtime.audit")


def _task_event(task):
    return {
        "type": "task",
        "id": str(task.id),
        "status": task.status,
        "task_type": task.task_type,
        "attempts": task.attempts,
        "updated_at": task.updated_at.isoformat(),
    }


def _audit_event(event):
    return {
        "type": "audit",
        "id": str(event.id),
        "event_type": event.event_type,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "outcome": event.outcome,
        "created_at": event.created_at.isoformat(),
    }


def _parse_since(value):
    if not value:
        return timezone.now()
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return timezone.now()
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


def _postgres_conninfo():
    config = settings.DATABASES["default"]
    kwargs = {
        "dbname": config.get("NAME", ""),
        "user": config.get("USER", ""),
        "password": config.get("PASSWORD", ""),
        "host": config.get("HOST", ""),
        "port": config.get("PORT", ""),
    }
    options = config.get("OPTIONS") or {}
    kwargs.update({key: value for key, value in options.items() if isinstance(key, str)})
    return kwargs


def _listen_notifications(timeout):
    try:
        import psycopg
    except ImportError:
        return None
    try:
        conn = psycopg.connect(**_postgres_conninfo(), autocommit=True)
        conn.execute(f"LISTEN {CHANNEL}")
    except Exception:
        return None
    return conn


def _scope_filter(tenant_id, project_id):
    from core.models import AuditEvent, Task

    return (
        Task.objects.filter(tenant_id=tenant_id, project_id=project_id),
        AuditEvent.objects.filter(tenant_id=tenant_id, project_id=project_id),
    )


def _collect_events(tenant_id, project_id, since, audit_visible, limit):
    tasks, audit = _scope_filter(tenant_id, project_id)
    items = []
    for task in tasks.filter(updated_at__gt=since).order_by("updated_at", "id")[:limit]:
        items.append((task.updated_at, _task_event(task)))
    if audit_visible:
        for event in audit.filter(created_at__gt=since).order_by("created_at", "id")[:limit]:
            items.append((event.created_at, _audit_event(event)))
    items.sort(key=lambda item: (item[0], item[1].get("id", "")))
    return items[:limit]


def event_stream(tenant_id, project_id, since, audit_visible, max_events=None):
    """Yield SSE frames for new project events.

    ``max_events`` is a bounded catch-up used by tests and by clients that only
    need a finite replay. The default is an unbounded live stream terminated by
    the client disconnecting.
    """

    cursor = _parse_since(since)
    emitted = 0
    started = time.monotonic()
    poll_seconds = float(getattr(settings, "REALTIME_STREAM_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    poll_seconds = min(max(poll_seconds, 0.1), 30.0)
    max_seconds = float(getattr(settings, "REALTIME_STREAM_MAX_SECONDS", MAX_STREAM_SECONDS))
    max_seconds = min(max(max_seconds, 1.0), 86400.0)

    yield "retry: 3000\n\n"
    yield f"event: ready\ndata: {json.dumps({'cursor': cursor.isoformat()})}\n\n"

    listener = None
    if connection.vendor == "postgresql":
        listener = _listen_notifications(15)

    try:
        while time.monotonic() - started < max_seconds:
            if listener is not None:
                # LISTEN is the wakeup source; the query below is the source of
                # truth, so a lost notification can never lose an event.
                try:
                    for notify in listener.notifies(timeout=15, stop_after=1):
                        try:
                            payload = json.loads(notify.payload)
                        except ValueError:
                            continue
                        if (
                            str(payload.get("tenant_id")) == str(tenant_id)
                            and str(payload.get("project_id")) == str(project_id)
                        ):
                            break
                except Exception:
                    listener = None
            else:
                time.sleep(poll_seconds)

            for timestamp, event in _collect_events(
                tenant_id,
                project_id,
                cursor,
                audit_visible,
                max_events or 50,
            ):
                cursor = max(cursor, timestamp)
                emitted += 1
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                event_id = (
                    event["updated_at"] if event["type"] == "task" else event["created_at"]
                )
                yield f"id: {event_id}\n"
                yield f"event: {event['type']}\ndata: {data}\n\n"
                if max_events is not None and emitted >= max_events:
                    return
            yield ": keep-alive\n\n"
            if max_events is not None and emitted >= max_events:
                return
    finally:
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
