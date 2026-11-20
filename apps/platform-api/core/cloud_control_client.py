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

    def _request(
        self,
        method: str,
        path: str,
        document: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=document)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Cloud Control response must be an object")
            return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise CloudControlDispatchError("Cloud Control API request failed") from exc

    def register_agent(
        self,
        agent_id: UUID,
        installation_id: UUID,
        role: str,
        cluster_id: UUID | None = None,
    ) -> None:
        if role not in {"eph", "target"}:
            raise ValueError("Agent role is invalid")
        if (role == "target" and cluster_id is None) or (
            role == "eph" and cluster_id is not None
        ):
            raise ValueError("Agent cluster scope is invalid")
        self._request(
            "POST",
            "/api/v1/control/agents/register",
            {
                "agent_id": str(agent_id),
                "installation_id": str(installation_id),
                "role": role,
                "cluster_id": str(cluster_id) if cluster_id else None,
            },
        )

    def create_deployment(
        self,
        deployment_id: UUID,
        installation_id: UUID,
        cluster_id: UUID,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "deployment_id": str(deployment_id),
            "installation_id": str(installation_id),
            "cluster_id": str(cluster_id),
        }
        if spec is not None:
            if not isinstance(spec, dict):
                raise ValueError("Deployment spec must be an object")
            body["spec"] = spec
        return self._request(
            "POST",
            "/api/v1/control/deployments",
            body,
        )

    def reconcile_deployments(self, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/control/deployments/reconcile",
            {"limit": limit},
        )

    def get_deployment(self, deployment_id: UUID) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/control/deployments/{deployment_id}",
        )

    def get_deployment_preflight(self, deployment_id: UUID) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/control/deployments/{deployment_id}/preflight",
        )

    def get_deployment_action_result(
        self, deployment_id: UUID, action: str
    ) -> dict[str, Any]:
        """Return the newest command result the EPH reported for one action."""

        return self._request(
            "GET",
            f"/api/v1/control/deployments/{deployment_id}/actions/{action}/result",
        )

    def get_command(self, command_id: UUID) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/control/commands/{command_id}",
        )

    def get_agent(self, agent_id: UUID) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/control/agents/{agent_id}",
        )

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
