from __future__ import annotations

from typing import Any

import httpx

from .config import AgentSettings


class AgentTransportError(RuntimeError):
    pass


class AgentControlClient:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.control_url,
            verify=settings.ca_file,
            cert=(settings.cert_file, settings.key_file),
            timeout=settings.request_timeout_seconds,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=payload)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentTransportError(f"Control API request failed: {path}") from exc
        if not isinstance(document, dict):
            raise AgentTransportError("Control API returned a non-object response")
        return document

    def heartbeat(self, sequence: int, readiness: str, capabilities: list[str]) -> None:
        identity = {
            "agent_id": str(self.settings.agent_id),
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
        }
        self._post("/api/v1/agent-control/heartbeats", {
            "schema_version": "v1",
            "identity": identity,
            "sequence": sequence,
            "readiness": readiness,
            "capabilities": capabilities,
        })

    def claim(self) -> dict[str, Any] | None:
        response = self._post("/api/v1/agent-control/commands/claim", {
            "schema_version": "v1",
            "agent_id": str(self.settings.agent_id),
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
        })
        command = response.get("command")
        if command is not None and not isinstance(command, dict):
            raise AgentTransportError("Control API command must be an object or null")
        return command

    def report_result(self, result: dict[str, Any]) -> None:
        payload = {
            **result,
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
        }
        self._post("/api/v1/agent-control/commands/results", payload)
