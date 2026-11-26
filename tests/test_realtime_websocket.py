import pytest
from asgiref.sync import async_to_sync
from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from core.models import Project, ProjectMembership, Task, Tenant, TenantMembership
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone
from platform_api.routing import websocket_urlpatterns


def _session_cookie(user):
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    return f"sessionid={session.session_key}".encode()


def _fixture():
    user = get_user_model().objects.create_user(username="ws-user", password="test-password")
    tenant = Tenant.objects.create(name="WS tenant", slug="ws-tenant")
    project = Project.objects.create(tenant=tenant, name="WS project", slug="ws-project")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.ADMIN)
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.EDITOR)
    return user, tenant, project


def test_websocket_requires_authentication(db):
    user, tenant, project = _fixture()

    async def run():
        communicator = WebsocketCommunicator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
            f"/ws/tenants/{tenant.id}/projects/{project.id}/events/",
        )
        connected, _ = await communicator.connect()
        assert connected is False

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def _session_key(user):
    return _session_cookie(user).decode().split("=", 1)[1]


def test_websocket_accepts_a_session_subprotocol(db):
    user, tenant, project = _fixture()
    token = _session_key(user)

    async def run():
        communicator = WebsocketCommunicator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
            f"/ws/tenants/{tenant.id}/projects/{project.id}/events/",
            subprotocols=["aiagentstack", f"session.{token}"],
        )
        connected, subprotocol = await communicator.connect()
        assert connected is True
        assert subprotocol == "aiagentstack"
        ready = await communicator.receive_json_from(timeout=2)
        assert ready["event"] == "ready"
        await communicator.disconnect()

    async_to_sync(run)()


def test_websocket_rejects_a_non_member(db):
    user, tenant, project = _fixture()
    outsider = get_user_model().objects.create_user(
        username="ws-outsider", password="test-password"
    )
    cookie = _session_cookie(outsider)

    async def run():
        communicator = WebsocketCommunicator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
            f"/ws/tenants/{tenant.id}/projects/{project.id}/events/",
            headers=[(b"cookie", cookie)],
            subprotocols=["aiagentstack"],
        )
        connected, _ = await communicator.connect()
        assert connected is False
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_websocket_streams_a_task_event():
    user, tenant, project = _fixture()
    cookie = _session_cookie(user)

    async def run():
        communicator = WebsocketCommunicator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
            f"/ws/tenants/{tenant.id}/projects/{project.id}/events/",
            headers=[(b"cookie", cookie)],
            subprotocols=["aiagentstack"],
        )
        connected, subprotocol = await communicator.connect()
        assert connected is True
        assert subprotocol == "aiagentstack"
        ready = await communicator.receive_json_from(timeout=2)
        assert ready["event"] == "ready"
        task = await database_sync_to_async(Task.objects.create)(
            tenant=tenant,
            project=project,
            created_by=user,
            task_type="demo.echo",
            payload={"message": "ws"},
            idempotency_key="ws-task-1",
            available_at=timezone.now(),
        )
        event = await communicator.receive_json_from(timeout=5)
        assert event["event"] == "task"
        assert event["data"]["id"] == str(task.id)
        await communicator.disconnect()

    async_to_sync(run)()
