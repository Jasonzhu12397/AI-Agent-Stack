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
