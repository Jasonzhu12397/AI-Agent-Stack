import json
from uuid import uuid4

import httpx
import pytest
from core.cloud_control_client import CloudControlClient


def _client(tmp_path, transport):
    files = []
    for name in ("ca.crt", "client.crt", "client.key"):
        path = tmp_path / name
        path.write_text("test", encoding="utf-8")
        files.append(str(path))
    return CloudControlClient(
        "https://cloud-control.example.test",
        files[0],
        files[1],
        files[2],
        transport=transport,
    )


def test_enqueue_command_uses_mtls_client_and_returns_command_uuid(tmp_path):
    expected = uuid4()
    def handler(request):
        assert request.url.path == "/api/v1/control/commands"
        return httpx.Response(201, json={"command_id": str(expected), "status": "queued"})
    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        result = client.enqueue_command(
            uuid4(), uuid4(), "target", "target.cluster.observe",
            "idempotency-key-0001", {}, "a" * 64,
        )
        assert result == expected
    finally:
        client.close()


def test_enqueue_command_rejects_missing_approval_digest(tmp_path):
    client = _client(tmp_path, httpx.MockTransport(lambda request: httpx.Response(500)))
    try:
        with pytest.raises(ValueError):
            client.enqueue_command(
                uuid4(), uuid4(), "target", "target.cluster.observe",
                "idempotency-key-0001", {}, "",
            )
    finally:
        client.close()



def test_cloud_control_registers_agent_and_creates_deployment(tmp_path):
    agent_id = uuid4()
    installation_id = uuid4()
    cluster_id = uuid4()
    deployment_id = uuid4()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/agents/register"):
            return httpx.Response(200, json={"registered": True})
        return httpx.Response(
            200,
            json={
                "created": True,
                "deployment_id": str(deployment_id),
                "phase": "not_started",
                "sequence": 0,
            },
        )

    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        client.register_agent(agent_id, installation_id, "target", cluster_id)
        result = client.create_deployment(
            deployment_id,
            installation_id,
            cluster_id,
            {"cluster_name": "edge-01", "worker_replicas": 3},
        )
        assert result["phase"] == "not_started"
        assert calls[0][0] == "POST"
        assert calls[0][1] == "/api/v1/control/agents/register"
        assert calls[0][2]["agent_id"] == str(agent_id)
        assert calls[1][1] == "/api/v1/control/deployments"
        assert calls[1][2]["cluster_id"] == str(cluster_id)
        assert calls[1][2]["spec"]["cluster_name"] == "edge-01"
    finally:
        client.close()


def test_cloud_control_status_methods_use_platform_api_routes(tmp_path):
    agent_id = uuid4()
    command_id = uuid4()
    deployment_id = uuid4()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "queued"})

    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        client.get_agent(agent_id)
        client.get_command(command_id)
        client.get_deployment(deployment_id)
        assert paths == [
            f"/api/v1/control/agents/{agent_id}",
            f"/api/v1/control/commands/{command_id}",
            f"/api/v1/control/deployments/{deployment_id}",
        ]
    finally:
        client.close()
