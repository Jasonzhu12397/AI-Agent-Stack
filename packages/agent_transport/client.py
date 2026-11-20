from __future__ import annotations

import hashlib
import json
from typing import Any

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

    def _identity(self) -> dict[str, Any]:
        return {
            "agent_id": str(self.settings.agent_id),
            "role": self.settings.role,
            "installation_id": str(self.settings.installation_id),
            "cluster_id": str(self.settings.cluster_id) if self.settings.cluster_id else None,
            "instance_id": self.settings.instance_id,
        }

    def claim_work(self, kind: str | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"schema_version": "v1", **self._identity()}
        if kind:
            payload["kind"] = kind
        response = self._post("/api/v1/agent-control/work/claim", payload)
        if response.get("work") is None:
            return None
        work = response["work"]
        if not isinstance(work, dict):
            raise AgentTransportError("Control API work must be an object or null")
        token = response.get("lease_token")
        if not isinstance(token, str) or not token:
            raise AgentTransportError("Control API work lease token is missing")
        skills = response.get("skills")
        plugins = response.get("plugins")
        return {
            "work": work,
            "skills": skills if isinstance(skills, list) else [],
            "plugins": plugins if isinstance(plugins, list) else [],
            "lease_token": token,
            "lease_seconds": response.get("lease_seconds"),
        }

    def renew_work_lease(self, work: dict[str, Any], lease_token: str) -> dict[str, Any]:
        response = self._post("/api/v1/agent-control/work/lease/renew", {
            "schema_version": "v1",
            **self._identity(),
            "work_id": work["work_id"],
            "lease_token": lease_token,
        })
        if response.get("renewed") is not True:
            raise AgentTransportError("Work lease renewal was not confirmed")
        return response

    def report_work_result(
        self,
        work: dict[str, Any],
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._post("/api/v1/agent-control/work/result", {
            "schema_version": "v1",
            **self._identity(),
            "work_id": work["work_id"],
            "status": status,
            "result": result,
        })
        if response.get("accepted") is not True:
            raise AgentTransportError("Work result was not accepted")
        return response

    def claim_task(self) -> dict[str, Any] | None:
        response = self._post("/api/v1/agent-control/tasks/claim", {
            "schema_version": "v1",
            **self._identity(),
        })
        if response.get("task") is None:
            return None
        task = response["task"]
        if not isinstance(task, dict):
            raise AgentTransportError("Control API task must be an object or null")
        token = response.get("lease_token")
        if not isinstance(token, str) or not token:
            raise AgentTransportError("Control API task lease token is missing")
        skills = response.get("skills")
        plugins = response.get("plugins")
        return {
            "task": task,
            "skills": skills if isinstance(skills, list) else [],
            "plugins": plugins if isinstance(plugins, list) else [],
            "lease_token": token,
            "lease_seconds": response.get("lease_seconds"),
        }

    def renew_task_lease(self, task: dict[str, Any], lease_token: str) -> dict[str, Any]:
        response = self._post("/api/v1/agent-control/tasks/lease/renew", {
            "schema_version": "v1",
            **self._identity(),
            "task_id": task["task_id"],
            "lease_token": lease_token,
        })
        if response.get("renewed") is not True:
            raise AgentTransportError("Task lease renewal was not confirmed")
        return response

    def report_task_result(
        self,
        task: dict[str, Any],
        lease_token: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._post("/api/v1/agent-control/tasks/result", {
            "schema_version": "v1",
            **self._identity(),
            "task_id": task["task_id"],
            "lease_token": lease_token,
            "status": status,
            "result": result,
        })
        if response.get("accepted") is not True:
            raise AgentTransportError("Task result was not accepted")
        return response

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
