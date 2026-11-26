"""Channels consumer for project task and audit events.

The consumer holds one WebSocket per console client. On SQLite it re-queries
the project every second; on PostgreSQL the same query runs after a LISTEN
wakeup would be ideal, but keeping the loop simple here still gives clients a
push channel rather than a browser-side polling loop. The HTTP SSE endpoint
stays available for clients that cannot open a WebSocket.
"""

from __future__ import annotations

import asyncio
import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from core.models import Project, Tenant, TenantMembership
from core.realtime import _collect_events, _parse_since

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0


def _resolve_scope(user, tenant_id, project_id, session_token=None):
    if user is None or not getattr(user, "is_authenticated", False):
        if not session_token:
            return None
        # WebSocket clients that cannot rely on a partitioned cookie pass the
        # same server-side session key the console sends as X-Session-Token,
        # carried in a WebSocket subprotocol rather than in the URL.
        session = SessionStore(session_key=session_token)
        if not session.exists(session_token):
            return None
        user_id = session.get(SESSION_KEY)
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            return None
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    tenant = (
        Tenant.objects.filter(memberships__user=user, pk=tenant_id)
        .distinct()
        .first()
    )
    if tenant is None:
        return None
    is_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project = Project.objects.filter(tenant=tenant, pk=project_id)
    if not is_admin:
        project = project.filter(memberships__user=user)
    project = project.distinct().first()
    if project is None:
        return None
    return {"audit_visible": is_admin}


def _read_events(tenant_id, project_id, since, audit_visible):
    cursor = _parse_since(since)
    events = []
    for timestamp, event in _collect_events(
        tenant_id,
        project_id,
        cursor,
        audit_visible,
        200,
    ):
        events.append({"cursor": timestamp.isoformat(), "event": event})
    next_cursor = events[-1]["cursor"] if events else cursor.isoformat()
    return next_cursor, events


class ProjectEventConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.tenant_id = self.scope["url_route"]["kwargs"]["tenant_id"]
        self.project_id = self.scope["url_route"]["kwargs"]["project_id"]
        self._task = None
        self._running = True
        self._since = ""
        protocols = self.scope.get("subprotocols") or []
        session_token = ""
        for protocol in protocols:
            if protocol.startswith("session."):
                session_token = protocol[len("session.") :]
                break
        scope = await database_sync_to_async(_resolve_scope)(
            self.scope.get("user"),
            self.tenant_id,
            self.project_id,
            session_token,
        )
        if scope is None:
            # 4403 is application-defined: the browser must not retry a
            # WebSocket that failed authorization.
            await self.close(code=4403)
            return
        self.audit_visible = scope["audit_visible"]
        selected = "aiagentstack" if "aiagentstack" in protocols else None
        await self.accept(subprotocol=selected)
        cursor = _parse_since(self._since or None)
        # Start the polling loop from the same cursor announced to the client.
        # Otherwise an event committed between accept and the first poll is
        # silently skipped.
        self._since = cursor.isoformat()
        await self.send(
            json.dumps(
                {
                    "event": "ready",
                    "cursor": cursor.isoformat(),
                },
                separators=(",", ":"),
            )
        )
        self._task = asyncio.create_task(self._stream())

    async def disconnect(self, close_code):
        self._running = False
        if self._task is not None:
            self._task.cancel()

    async def receive(self, text_data=None, bytes_data=None):
        # A client may send {"since": "..."} after a manual reconnect.
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except ValueError:
            return
        if isinstance(payload, dict) and isinstance(payload.get("since"), str):
            self._since = payload["since"]

    async def _stream(self):
        cursor = self._since or None
        last_heartbeat = timezone.now()
        try:
            while self._running:
                cursor, events = await database_sync_to_async(_read_events)(
                    self.tenant_id,
                    self.project_id,
                    cursor,
                    self.audit_visible,
                )
                for item in events:
                    if not self._running:
                        return
                    await self.send(
                        json.dumps(
                            {
                                "event": item["event"]["type"],
                                "data": item["event"],
                                "cursor": item["cursor"],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                now = timezone.now()
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    await self.send(json.dumps({"event": "heartbeat"}, separators=(",", ":")))
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            return
