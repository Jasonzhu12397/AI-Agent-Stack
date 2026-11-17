from __future__ import annotations

from typing import Any

import hashlib
import json

import httpx

from .config import AgentSettings


class AgentTransportError(RuntimeError):
    pass


class AgentControlClient:
    def __init__(
        self,
        settings: AgentSettings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.control_url,
            verify=settings.ca_file,
            cert=(settings.cert_file, settings.key_file),
            timeout=settings.request_timeout_seconds,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            follow_redirects=False,
            transport=transport,
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

    def check_ready(self) -> bool:
        try:
            response = self._client.get("/readyz")
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentTransportError("Control API readiness check failed") from exc
        return isinstance(document, dict) and document.get("status") == "ready"

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

    def renew_lease(self, command: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/api/v1/agent-control/commands/lease/renew", {
            "schema_version": "v1",
            "agent_id": str(self.settings.agent_id),
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
            "command_id": command["command_id"],
            "lease_id": command["lease"]["lease_id"],
            "lease_token": command["lease"]["token"],
        })
        if response.get("renewed") is not True:
            raise AgentTransportError("Command lease renewal was not confirmed")
        return response

    def persist_checkpoint(
        self,
        command: dict[str, Any],
        phase: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        checkpoint = command.get("deployment_checkpoint")
        if not isinstance(checkpoint, dict):
            raise AgentTransportError("Deployment checkpoint is missing from command")
        encoded = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        response = self._post("/api/v1/agent-control/deployments/checkpoints", {
            "schema_version": "v1",
            "agent_id": str(self.settings.agent_id),
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
            "deployment_id": command["deployment_id"],
            "command_id": command["command_id"],
            "lease_id": command["lease"]["lease_id"],
            "lease_token": command["lease"]["token"],
            "expected_phase": checkpoint["phase"],
            "expected_sequence": checkpoint["sequence"],
            "phase": phase,
            "state_digest": hashlib.sha256(encoded).hexdigest(),
            "evidence": evidence,
        })
        if response.get("persisted") is not True:
            raise AgentTransportError("Deployment checkpoint persistence was not confirmed")
        return response

    def report_result(self, result: dict[str, Any]) -> None:
        payload = {
            **result,
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
        }
        self._post("/api/v1/agent-control/commands/results", payload)
