from __future__ import annotations

import hashlib
import json
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


MAX_BODY_BYTES = 1024 * 1024
LEASE_SECONDS = 90


class ControlApiError(ValueError):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def _certificate_common_name(certificate: dict[str, Any] | None) -> str | None:
    if not certificate:
        return None
    for rdn in certificate.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def _parse_uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ControlApiError(f"{field}_INVALID") from exc


def create_handler(database_url: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CloudControlAgent/1"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, document: dict[str, Any]) -> None:
            payload = _json_bytes(document)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ControlApiError("CONTENT_LENGTH_INVALID") from exc
            if length < 1 or length > MAX_BODY_BYTES:
                raise ControlApiError("REQUEST_SIZE_INVALID")
            try:
                document = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ControlApiError("JSON_INVALID") from exc
            if not isinstance(document, dict):
                raise ControlApiError("JSON_OBJECT_REQUIRED")
            return document

        def _authenticated_agent(self, document: dict[str, Any]) -> dict[str, Any]:
            identity = document.get("identity", document)
            if not isinstance(identity, dict):
                raise ControlApiError("AGENT_IDENTITY_REQUIRED")
            agent_id = _parse_uuid(identity.get("agent_id"), "AGENT_ID")
            certificate = self.connection.getpeercert()
            common_name = _certificate_common_name(certificate)
            if common_name != str(agent_id):
                raise ControlApiError("CLIENT_CERTIFICATE_IDENTITY_MISMATCH")
            role = identity.get("role")
            installation_id = _parse_uuid(identity.get("installation_id"), "INSTALLATION_ID")
            cluster_raw = identity.get("cluster_id")
            cluster_id = _parse_uuid(cluster_raw, "CLUSTER_ID") if cluster_raw else None
            instance_id = identity.get("instance_id")
            if role not in ("eph", "target") or not isinstance(instance_id, str) or not instance_id:
                raise ControlApiError("AGENT_IDENTITY_INVALID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT agent_id, installation_id, role, cluster_id, enabled
                    FROM agent_control_agents WHERE agent_id = %s
                    """,
                    (agent_id,),
                ).fetchone()
            if not row or not row["enabled"]:
                raise ControlApiError("AGENT_NOT_REGISTERED")
            if (
                row["installation_id"] != installation_id
                or row["role"] != role
                or row["cluster_id"] != cluster_id
            ):
                raise ControlApiError("AGENT_REGISTRATION_MISMATCH")
            return {
                "agent_id": agent_id,
                "installation_id": installation_id,
                "role": role,
                "cluster_id": cluster_id,
                "instance_id": instance_id,
            }

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"status": "healthy"})
                return
            if self.path == "/readyz":
                try:
                    with psycopg.connect(database_url) as connection:
                        connection.execute("SELECT 1").fetchone()
                except psycopg.Error:
                    self._send(503, {"status": "not_ready"})
                    return
                self._send(200, {"status": "ready"})
                return
            self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                document = self._body()
                if self.path == "/api/v1/agent-control/heartbeats":
                    self._heartbeat(document)
                    return
                if self.path == "/api/v1/agent-control/commands/claim":
                    self._claim(document)
                    return
                if self.path == "/api/v1/agent-control/commands/results":
                    self._result(document)
                    return
                if self.path == "/api/v1/control/commands":
                    self._enqueue(document)
                    return
                self._send(404, {"error": "NOT_FOUND"})
            except ControlApiError as exc:
                self._send(400, {"error": str(exc)})
            except psycopg.Error:
                self._send(503, {"error": "PERSISTENCE_UNAVAILABLE"})


        def _enqueue(self, document: dict[str, Any]) -> None:
            certificate = self.connection.getpeercert()
            if _certificate_common_name(certificate) != "platform-api":
                raise ControlApiError("PLATFORM_API_CERTIFICATE_REQUIRED")
            deployment_id = _parse_uuid(document.get("deployment_id"), "DEPLOYMENT_ID")
            target_agent_id = _parse_uuid(document.get("target_agent_id"), "TARGET_AGENT_ID")
            role = document.get("required_role")
            action = document.get("action")
            idempotency_key = document.get("idempotency_key")
            payload = document.get("payload", {})
            approval_digest = document.get("approval_digest")
            if role not in ("eph", "target"):
                raise ControlApiError("COMMAND_ROLE_INVALID")
            if not isinstance(action, str) or not action or len(action) > 128:
                raise ControlApiError("COMMAND_ACTION_INVALID")
            if (
                not isinstance(idempotency_key, str)
                or len(idempotency_key) < 16
                or len(idempotency_key) > 200
            ):
                raise ControlApiError("IDEMPOTENCY_KEY_INVALID")
            if not isinstance(payload, dict) or len(_json_bytes(payload)) > MAX_BODY_BYTES:
                raise ControlApiError("COMMAND_PAYLOAD_INVALID")
            if not isinstance(approval_digest, str) or len(approval_digest) != 64:
                raise ControlApiError("APPROVAL_DIGEST_REQUIRED")
            try:
                int(approval_digest, 16)
            except ValueError as exc:
                raise ControlApiError("APPROVAL_DIGEST_INVALID") from exc
            command_id = uuid4()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                agent = connection.execute(
                    "SELECT role, enabled, cluster_id "
                    "FROM agent_control_agents WHERE agent_id = %s",
                    (target_agent_id,),
                ).fetchone()
                deployment = connection.execute(
                    "SELECT deployment_id, cluster_id "
                    "FROM agent_control_deployments WHERE deployment_id = %s",
                    (deployment_id,),
                ).fetchone()
                if not agent or not agent["enabled"] or agent["role"] != role:
                    raise ControlApiError("TARGET_AGENT_NOT_ELIGIBLE")
                if not deployment:
                    raise ControlApiError("DEPLOYMENT_NOT_FOUND")
                if role == "target" and agent["cluster_id"] != deployment["cluster_id"]:
                    raise ControlApiError("TARGET_AGENT_CLUSTER_MISMATCH")
                connection.execute(
                    """
                    INSERT INTO agent_control_commands (
                        command_id, deployment_id, target_agent_id, required_role,
                        action, idempotency_key, payload, status, approval_digest
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                    """,
                    (
                        command_id, deployment_id, target_agent_id, role, action,
                        idempotency_key, Jsonb(payload), approval_digest,
                    ),
                )
            self._send(201, {"command_id": str(command_id), "status": "queued"})

        def _heartbeat(self, document: dict[str, Any]) -> None:
            identity = self._authenticated_agent(document)
            sequence = document.get("sequence")
            readiness = document.get("readiness")
            capabilities = document.get("capabilities", [])
            if not isinstance(sequence, int) or sequence < 1:
                raise ControlApiError("HEARTBEAT_SEQUENCE_INVALID")
            if readiness not in ("ready", "degraded", "not_ready"):
                raise ControlApiError("HEARTBEAT_READINESS_INVALID")
            if not isinstance(capabilities, list) or len(capabilities) > 100:
                raise ControlApiError("HEARTBEAT_CAPABILITIES_INVALID")
            with psycopg.connect(database_url) as connection:
                updated = connection.execute(
                    """
                    UPDATE agent_control_agents
                    SET instance_id = %s, heartbeat_sequence = %s, readiness = %s,
                        capabilities = %s, last_seen = NOW(), updated_at = NOW()
                    WHERE agent_id = %s AND heartbeat_sequence < %s
                    """,
                    (
                        identity["instance_id"], sequence, readiness, Jsonb(capabilities),
                        identity["agent_id"], sequence,
                    ),
                ).rowcount
                if updated != 1:
                    raise ControlApiError("HEARTBEAT_SEQUENCE_STALE")
            self._send(200, {"accepted": True})

        def _claim(self, document: dict[str, Any]) -> None:
            identity = self._authenticated_agent(document)
            now = datetime.now(timezone.utc)
            lease_id = uuid4()
            lease_token = secrets.token_urlsafe(40)
            token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT command_id, deployment_id, target_agent_id, required_role,
                               action, idempotency_key, payload, created_at
                        FROM agent_control_commands
                        WHERE target_agent_id = %s AND required_role = %s
                          AND (
                              status = 'queued'
                              OR (status = 'leased' AND lease_expires_at <= NOW())
                          )
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """,
                        (identity["agent_id"], identity["role"]),
                    ).fetchone()
                    if row is None:
                        self._send(200, {"command": None})
                        return
                    deadline = now + timedelta(minutes=15)
                    expires = min(deadline, now + timedelta(seconds=LEASE_SECONDS))
                    connection.execute(
                        """
                        UPDATE agent_control_commands
                        SET status = 'leased', lease_id = %s, lease_token_hash = %s,
                            lease_expires_at = %s, issued_at = %s, deadline = %s,
                            updated_at = NOW()
                        WHERE command_id = %s
                        """,
                        (lease_id, token_hash, expires, now, deadline, row["command_id"]),
                    )
            self._send(200, {
                "command": {
                    "schema_version": "v1",
                    "command_id": str(row["command_id"]),
                    "idempotency_key": row["idempotency_key"],
                    "deployment_id": str(row["deployment_id"]),
                    "target_agent_id": str(row["target_agent_id"]),
                    "required_role": row["required_role"],
                    "action": row["action"],
                    "payload": row["payload"],
                    "issued_at": now.isoformat(),
                    "deadline": deadline.isoformat(),
                    "lease": {
                        "lease_id": str(lease_id),
                        "token": lease_token,
                        "issued_at": now.isoformat(),
                        "expires_at": expires.isoformat(),
                    },
                }
            })

        def _result(self, document: dict[str, Any]) -> None:
            agent_id = _parse_uuid(document.get("agent_id"), "AGENT_ID")
            identity_document = {
                "agent_id": str(agent_id),
                "role": document.get("role"),
                "installation_id": document.get("installation_id"),
                "cluster_id": document.get("cluster_id"),
                "instance_id": document.get("instance_id"),
            }
            identity = self._authenticated_agent(identity_document)
            command_id = _parse_uuid(document.get("command_id"), "COMMAND_ID")
            lease_id = _parse_uuid(document.get("lease_id"), "LEASE_ID")
            lease_token = document.get("lease_token")
            status = document.get("status")
            if not isinstance(lease_token, str) or len(lease_token) < 32:
                raise ControlApiError("LEASE_TOKEN_INVALID")
            if status not in ("succeeded", "failed", "blocked", "cancelled"):
                raise ControlApiError("RESULT_STATUS_INVALID")
            token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
            with psycopg.connect(database_url) as connection:
                updated = connection.execute(
                    """
                    UPDATE agent_control_commands
                    SET status = %s, result = %s, updated_at = NOW()
                    WHERE command_id = %s AND target_agent_id = %s
                      AND lease_id = %s AND lease_token_hash = %s
                      AND status = 'leased' AND lease_expires_at > NOW()
                    """,
                    (
                        status,
                        Jsonb({
                            key: value
                            for key, value in document.items()
                            if key != "lease_token"
                        }),
                        command_id,
                        identity["agent_id"],
                        lease_id, token_hash,
                    ),
                ).rowcount
                if updated != 1:
                    raise ControlApiError("COMMAND_LEASE_NOT_VALID")
            self._send(200, {"accepted": True})

    return Handler


def serve(
    database_url: str,
    host: str,
    port: int,
    cert_file: str,
    key_file: str,
    client_ca_file: str,
) -> None:
    server = ThreadingHTTPServer((host, port), create_handler(database_url))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    context.load_verify_locations(cafile=client_ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
