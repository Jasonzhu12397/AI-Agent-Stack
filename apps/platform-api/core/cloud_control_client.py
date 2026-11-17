from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx


class CloudControlDispatchError(RuntimeError):
    pass


class CloudControlClient:
    def __init__(
        self,
        base_url: str,
        ca_file: str,
        client_cert_file: str,
        client_key_file: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Cloud Control API URL must use HTTPS")
        for value in (ca_file, client_cert_file, client_key_file):
            if not Path(value).is_file():
                raise ValueError("Cloud Control mTLS file is missing")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            verify=ca_file,
            cert=(client_cert_file, client_key_file),
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def enqueue_command(
        self,
        deployment_id: UUID,
        target_agent_id: UUID,
        required_role: str,
        action: str,
        idempotency_key: str,
        payload: dict[str, Any],
        approval_digest: str,
    ) -> UUID:
        if required_role not in {"eph", "target"}:
            raise ValueError("Agent role is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", approval_digest):
            raise ValueError("A verified approval digest is required")
        if not 16 <= len(idempotency_key) <= 200:
            raise ValueError("Idempotency key length is invalid")
        try:
            response = self._client.post(
                "/api/v1/control/commands",
                json={
                    "deployment_id": str(deployment_id),
                    "target_agent_id": str(target_agent_id),
                    "required_role": required_role,
                    "action": action,
                    "idempotency_key": idempotency_key,
                    "payload": payload,
                    "approval_digest": approval_digest,
                },
            )
            response.raise_for_status()
            document = response.json()
            return UUID(document["command_id"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise CloudControlDispatchError("Cloud Control command submission failed") from exc
