import json
from types import SimpleNamespace

import pytest
from core.models import Project, ProjectMembership, Task, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient


@pytest.fixture
def workspace(db):
    user = get_user_model().objects.create_user(
        username="mcp-server-user",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="MCP Server tenant", slug="mcp-server-tenant")
    project = Project.objects.create(tenant=tenant, name="MCP Server project", slug="mcp-project")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.EDITOR)
    return SimpleNamespace(user=user, tenant=tenant, project=project)


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _url(workspace):
    return reverse(
        "mcp-server",
        kwargs={"tenant_id": workspace.tenant.id, "project_id": workspace.project.id},
    )


def _rpc(client, workspace, payload):
    return client.post(_url(workspace), data=json.dumps(payload), content_type="application/json")


def test_initialize_and_tools_list(workspace):
    response = _rpc(
        _client(workspace.user),
        workspace,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "test"}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["serverInfo"]["name"] == "ai-agent-stack"

    response = _rpc(
        _client(workspace.user),
        workspace,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"platform_list_tasks", "platform_get_task", "platform_create_task"} <= names
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_create_list_and_get_task(workspace):
    client = _client(workspace.user)
    response = _rpc(
        client,
        workspace,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "platform_create_task",
                "arguments": {
                    "task_type": "demo.echo",
                    "payload": {"text": "from mcp"},
                    "idempotency_key": "mcp-create-1",
                },
            },
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["created"] is True
    task_id = payload["task"]["id"]
    assert Task.objects.filter(pk=task_id, project=workspace.project).exists()

    response = _rpc(
        client,
        workspace,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "platform_list_tasks", "arguments": {"limit": 5}},
        },
    )
    tasks = response.json()["result"]["structuredContent"]["tasks"]
    assert [task["id"] for task in tasks] == [task_id]

    response = _rpc(
        client,
        workspace,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "platform_get_task", "arguments": {"task_id": task_id}},
        },
    )
    assert response.json()["result"]["structuredContent"]["task"]["id"] == task_id


def test_create_task_rejects_idempotency_reuse_with_different_payload(workspace):
    client = _client(workspace.user)
    first = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "platform_create_task",
            "arguments": {
                "task_type": "demo.echo",
                "payload": {"text": "first"},
                "idempotency_key": "mcp-reuse",
            },
        },
    }
    assert _rpc(client, workspace, first).json()["result"]["structuredContent"]["created"] is True
    second = json.loads(json.dumps(first))
    second["id"] = 7
    second["params"]["arguments"]["payload"] = {"text": "second"}
    response = _rpc(client, workspace, second)
    error = response.json()["error"]
    assert error["code"] == -32602
    assert "idempotency_key" in error["message"]


def test_unknown_method_and_notification(workspace):
    response = _rpc(
        _client(workspace.user),
        workspace,
        {"jsonrpc": "2.0", "id": 8, "method": "no/such/method"},
    )
    body = response.json()
    assert body["error"]["code"] == -32601
    assert body["id"] == 8

    response = _rpc(
        _client(workspace.user),
        workspace,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202


def test_invalid_json_returns_parse_error(workspace):
    client = _client(workspace.user)
    response = client.post(_url(workspace), data="{", content_type="application/json")
    body = response.json()
    assert body["error"]["code"] == -32700
    assert body["id"] is None


def test_non_member_cannot_reach_the_server(workspace):
    outsider = get_user_model().objects.create_user(
        username="mcp-outsider",
        password="test-password",
    )
    response = _rpc(
        _client(outsider),
        workspace,
        {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
    )
    assert response.status_code == 404
