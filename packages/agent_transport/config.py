from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID


class ConfigurationError(ValueError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _file(name: str) -> str:
    value = _required(name)
    path = Path(value)
    if not path.is_file():
        raise ConfigurationError(f"{name} does not reference a readable file")
    return str(path)


@dataclass(frozen=True)
class AgentSettings:
    control_url: str
    agent_id: UUID
    installation_id: UUID
    instance_id: str
    role: str
    cluster_id: UUID | None
    ca_file: str
    cert_file: str
    key_file: str
    heartbeat_seconds: float = 10.0
    poll_seconds: float = 5.0
    request_timeout_seconds: float = 15.0

    @classmethod
    def from_environment(cls, expected_role: str) -> "AgentSettings":
        raw_url = _required("AGENT_CONTROL_URL")
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigurationError("AGENT_CONTROL_URL must be an absolute HTTPS URL")
        try:
            agent_id = UUID(_required("AGENT_ID"))
            installation_id = UUID(_required("AGENT_INSTALLATION_ID"))
            cluster_raw = os.environ.get("AGENT_CLUSTER_ID", "").strip()
            cluster_id = UUID(cluster_raw) if cluster_raw else None
        except ValueError as exc:
            raise ConfigurationError("Agent identity values must be valid UUIDs") from exc
        role = _required("AGENT_ROLE")
        if role != expected_role:
            raise ConfigurationError(f"Agent role mismatch: expected {expected_role}, got {role}")
        if role == "target" and cluster_id is None:
            raise ConfigurationError("Target Agent requires AGENT_CLUSTER_ID")
        if role == "eph" and cluster_id is not None:
            raise ConfigurationError("EPH Agent must not be assigned the target cluster ID")
        instance_id = _required("AGENT_INSTANCE_ID")
        if len(instance_id) > 128:
            raise ConfigurationError("AGENT_INSTANCE_ID exceeds 128 characters")
        try:
            heartbeat = float(os.environ.get("AGENT_HEARTBEAT_SECONDS", "10"))
            poll = float(os.environ.get("AGENT_POLL_SECONDS", "5"))
            timeout = float(os.environ.get("AGENT_REQUEST_TIMEOUT_SECONDS", "15"))
        except ValueError as exc:
            raise ConfigurationError("Agent timing values must be numeric") from exc
        if min(heartbeat, poll, timeout) <= 0 or max(heartbeat, poll, timeout) > 300:
            raise ConfigurationError("Agent timing values must be in the range (0, 300]")
        return cls(
            control_url=raw_url.rstrip("/"),
            agent_id=agent_id,
            installation_id=installation_id,
            instance_id=instance_id,
            role=role,
            cluster_id=cluster_id,
            ca_file=_file("AGENT_TLS_CA_FILE"),
            cert_file=_file("AGENT_TLS_CERT_FILE"),
            key_file=_file("AGENT_TLS_KEY_FILE"),
            heartbeat_seconds=heartbeat,
            poll_seconds=poll,
            request_timeout_seconds=timeout,
        )
