from __future__ import annotations

import hashlib
import json
import secrets
import ssl
from datetime import UTC, datetime, timedelta
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

EPH_ACTIONS = frozenset({
    "eph.preflight",
    "eph.source.observe",
    "eph.capi.pivot.dry_run",
    "eph.capi.pivot",
})
TARGET_ACTIONS = frozenset({
    "target.cluster.observe",
    "target.capi.observe",
    "target.metal3.observe",
    "target.metal3.verify_ownership",
    "target.worker.ntp.validate",
    "target.ntp.control_plane.render",
    "target.ntp.control_plane.verify_bootstrap",
    "target.ntp.internal.probe",
    "target.handoff.evaluate",
})


def _validate_action_role(role: str, action: str) -> None:
    allowed = EPH_ACTIONS if role == "eph" else TARGET_ACTIONS if role == "target" else frozenset()
    if action not in allowed:
        raise ControlApiError("COMMAND_ACTION_NOT_ALLOWED_FOR_AGENT_ROLE")


CHECKPOINT_PHASES = (
    "eph_single_node_ready",
    "target_control_plane_ready",
    "target_agent_ready",
    "capi_pivot_verified",
    "bmh_registry_migrated",
    "eph_cleanup_verified",
    "worker_provisioning",
    "workers_ready",
    "completed",
)


def _validate_checkpoint_actor_transition(
    role: str,
    current_phase: str,
    next_phase: str,
) -> None:
    if current_phase == "not_started":
        current_index = -1
    else:
        try:
            current_index = CHECKPOINT_PHASES.index(current_phase)
        except ValueError as exc:
            raise ControlApiError("CHECKPOINT_CURRENT_PHASE_INVALID") from exc
    try:
        next_index = CHECKPOINT_PHASES.index(next_phase)
    except ValueError as exc:
        raise ControlApiError("CHECKPOINT_PHASE_INVALID") from exc
    if role == "eph":
        if current_index != -1 or next_index != 0:
            raise ControlApiError("EPH_CHECKPOINT_PHASE_NOT_ALLOWED")
        return
    if role != "target":
        raise ControlApiError("CHECKPOINT_AGENT_ROLE_INVALID")
    if next_index < 1 or next_index != current_index + 1:
        raise ControlApiError("TARGET_CHECKPOINT_TRANSITION_INVALID")



def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def _checkpoint_state_digest(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
            try:
                if self.path.startswith("/api/v1/control/deployments/"):
                    self._get_deployment(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/commands/"):
                    self._get_command(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/agents/"):
                    self._get_agent(self.path.rsplit("/", 1)[-1])
                    return
            except ControlApiError as exc:
                self._send(400, {"error": str(exc)})
                return
            except psycopg.Error:
                self._send(503, {"error": "PERSISTENCE_UNAVAILABLE"})
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
                if self.path == "/api/v1/agent-control/commands/lease/renew":
                    self._renew_lease(document)
                    return
                if self.path == "/api/v1/agent-control/deployments/checkpoints":
                    self._persist_checkpoint(document)
                    return
                if self.path == "/api/v1/agent-control/commands/results":
                    self._result(document)
                    return
                if self.path == "/api/v1/control/agents/register":
                    self._register_agent(document)
                    return
                if self.path == "/api/v1/control/deployments":
                    self._create_deployment(document)
                    return
                if self.path == "/api/v1/control/commands":
                    self._enqueue(document)
                    return
                self._send(404, {"error": "NOT_FOUND"})
            except ControlApiError as exc:
                self._send(400, {"error": str(exc)})
            except psycopg.Error:
                self._send(503, {"error": "PERSISTENCE_UNAVAILABLE"})



        def _require_platform_api(self) -> None:
            if _certificate_common_name(self.connection.getpeercert()) != "platform-api":
                raise ControlApiError("PLATFORM_API_CERTIFICATE_REQUIRED")

        def _register_agent(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            agent_id = _parse_uuid(document.get("agent_id"), "AGENT_ID")
            installation_id = _parse_uuid(
                document.get("installation_id"),
                "INSTALLATION_ID",
            )
            role = document.get("role")
            cluster_raw = document.get("cluster_id")
            cluster_id = _parse_uuid(cluster_raw, "CLUSTER_ID") if cluster_raw else None
            if role not in ("eph", "target"):
                raise ControlApiError("AGENT_ROLE_INVALID")
            if (role == "target" and cluster_id is None) or (
                role == "eph" and cluster_id is not None
            ):
                raise ControlApiError("AGENT_CLUSTER_SCOPE_INVALID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    INSERT INTO agent_control_agents (
                        agent_id, installation_id, role, cluster_id, enabled
                    )
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (agent_id) DO UPDATE
                    SET enabled = TRUE, updated_at = NOW()
                    WHERE agent_control_agents.installation_id = EXCLUDED.installation_id
                      AND agent_control_agents.role = EXCLUDED.role
                      AND agent_control_agents.cluster_id IS NOT DISTINCT FROM EXCLUDED.cluster_id
                    RETURNING agent_id
                    """,
                    (agent_id, installation_id, role, cluster_id),
                ).fetchone()
            if row is None:
                raise ControlApiError("AGENT_REGISTRATION_IDENTITY_CONFLICT")
            self._send(200, {"registered": True, "agent_id": str(agent_id)})

        def _create_deployment(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            deployment_id = _parse_uuid(
                document.get("deployment_id"),
                "DEPLOYMENT_ID",
            )
            installation_id = _parse_uuid(
                document.get("installation_id"),
                "INSTALLATION_ID",
            )
            cluster_id = _parse_uuid(document.get("cluster_id"), "CLUSTER_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO agent_control_deployments (
                            deployment_id, installation_id, cluster_id,
                            phase, sequence, state
                        )
                        VALUES (%s, %s, %s, 'not_started', 0, '{}'::jsonb)
                        ON CONFLICT (deployment_id) DO NOTHING
                        """,
                        (deployment_id, installation_id, cluster_id),
                    )
                    row = connection.execute(
                        """
                        SELECT installation_id, cluster_id, phase, sequence, state
                        FROM agent_control_deployments
                        WHERE deployment_id = %s
                        """,
                        (deployment_id,),
                    ).fetchone()
                    if (
                        row is None
                        or row["installation_id"] != installation_id
                        or row["cluster_id"] != cluster_id
                    ):
                        raise ControlApiError("DEPLOYMENT_IDENTITY_CONFLICT")
            self._send(200, {
                "created": True,
                "deployment_id": str(deployment_id),
                "phase": row["phase"],
                "sequence": row["sequence"],
            })

        def _get_deployment(self, raw_id: str) -> None:
            self._require_platform_api()
            deployment_id = _parse_uuid(raw_id, "DEPLOYMENT_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT deployment_id, installation_id, cluster_id, phase,
                           sequence, state, updated_at
                    FROM agent_control_deployments
                    WHERE deployment_id = %s
                    """,
                    (deployment_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("DEPLOYMENT_NOT_FOUND")
            self._send(200, row)

        def _get_command(self, raw_id: str) -> None:
            self._require_platform_api()
            command_id = _parse_uuid(raw_id, "COMMAND_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT command_id, deployment_id, target_agent_id, required_role,
                           action, status, result, lease_expires_at, deadline,
                           approval_digest, created_at, updated_at
                    FROM agent_control_commands
                    WHERE command_id = %s
                    """,
                    (command_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("COMMAND_NOT_FOUND")
            self._send(200, row)

        def _get_agent(self, raw_id: str) -> None:
            self._require_platform_api()
            agent_id = _parse_uuid(raw_id, "AGENT_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT agent_id, installation_id, role, cluster_id, enabled,
                           instance_id, heartbeat_sequence, readiness,
                           capabilities, last_seen, updated_at
                    FROM agent_control_agents
                    WHERE agent_id = %s
                    """,
                    (agent_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("AGENT_NOT_FOUND")
            self._send(200, row)

        def _enqueue(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
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
            _validate_action_role(role, action)
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
            command_status = "queued"
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                agent = connection.execute(
                    "SELECT role, enabled, cluster_id, installation_id "
                    "FROM agent_control_agents WHERE agent_id = %s",
                    (target_agent_id,),
                ).fetchone()
                deployment = connection.execute(
                    "SELECT deployment_id, installation_id, cluster_id "
                    "FROM agent_control_deployments WHERE deployment_id = %s",
                    (deployment_id,),
                ).fetchone()
                if not agent or not agent["enabled"] or agent["role"] != role:
                    raise ControlApiError("TARGET_AGENT_NOT_ELIGIBLE")
                if not deployment:
                    raise ControlApiError("DEPLOYMENT_NOT_FOUND")
                if agent["installation_id"] != deployment["installation_id"]:
                    raise ControlApiError("AGENT_INSTALLATION_MISMATCH")
                if role == "target" and agent["cluster_id"] != deployment["cluster_id"]:
                    raise ControlApiError("TARGET_AGENT_CLUSTER_MISMATCH")
                inserted = connection.execute(
                    """
                    INSERT INTO agent_control_commands (
                        command_id, deployment_id, target_agent_id, required_role,
                        action, idempotency_key, payload, status, approval_digest
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING command_id
                    """,
                    (
                        command_id, deployment_id, target_agent_id, role, action,
                        idempotency_key, Jsonb(payload), approval_digest,
                    ),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        """
                        SELECT command_id, deployment_id, target_agent_id, required_role,
                               action, payload, approval_digest, status
                        FROM agent_control_commands WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if (
                        not existing
                        or existing["deployment_id"] != deployment_id
                        or existing["target_agent_id"] != target_agent_id
                        or existing["required_role"] != role
                        or existing["action"] != action
                        or existing["payload"] != payload
                        or existing["approval_digest"] != approval_digest
                    ):
                        raise ControlApiError("IDEMPOTENCY_KEY_CONFLICT")
                    command_id = existing["command_id"]
                    command_status = existing["status"]
            self._send(201, {"command_id": str(command_id), "status": command_status})

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
            now = datetime.now(UTC)
            lease_id = uuid4()
            lease_token = secrets.token_urlsafe(40)
            token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT c.command_id, c.deployment_id, c.target_agent_id,
                               c.required_role, c.action, c.idempotency_key,
                               c.payload, c.approval_digest, c.created_at, c.deadline,
                               d.phase AS deployment_phase,
                               d.sequence AS checkpoint_sequence,
                               d.state AS checkpoint_state
                        FROM agent_control_commands AS c
                        JOIN agent_control_deployments AS d
                          ON d.deployment_id = c.deployment_id
                        WHERE c.target_agent_id = %s AND c.required_role = %s
                          AND (c.deadline IS NULL OR c.deadline > NOW())
                          AND (
                              c.status = 'queued'
                              OR (c.status = 'leased' AND c.lease_expires_at <= NOW())
                          )
                        ORDER BY c.created_at
                        LIMIT 1
                        FOR UPDATE OF c SKIP LOCKED
                        """,
                        (identity["agent_id"], identity["role"]),
                    ).fetchone()
                    if row is None:
                        self._send(200, {"command": None})
                        return
                    deadline = row["deadline"] or now + timedelta(minutes=15)
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
                    "approval_digest": row["approval_digest"],
                    "deployment_checkpoint": {
                        "phase": row["deployment_phase"],
                        "sequence": row["checkpoint_sequence"],
                        "state": row["checkpoint_state"],
                    },
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


        def _renew_lease(self, document: dict[str, Any]) -> None:
            identity = self._authenticated_agent(document)
            command_id = _parse_uuid(document.get("command_id"), "COMMAND_ID")
            lease_id = _parse_uuid(document.get("lease_id"), "LEASE_ID")
            lease_token = document.get("lease_token")
            if not isinstance(lease_token, str) or len(lease_token) < 32:
                raise ControlApiError("LEASE_TOKEN_INVALID")
            token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    UPDATE agent_control_commands
                    SET lease_expires_at = LEAST(deadline, NOW() + (%s * INTERVAL '1 second')),
                        updated_at = NOW()
                    WHERE command_id = %s AND target_agent_id = %s
                      AND lease_id = %s AND lease_token_hash = %s
                      AND status = 'leased' AND lease_expires_at > NOW()
                      AND deadline > NOW()
                    RETURNING lease_expires_at
                    """,
                    (
                        LEASE_SECONDS,
                        command_id,
                        identity["agent_id"],
                        lease_id,
                        token_hash,
                    ),
                ).fetchone()
            if row is None:
                raise ControlApiError("COMMAND_LEASE_NOT_RENEWABLE")
            self._send(200, {"renewed": True, "expires_at": row["lease_expires_at"].isoformat()})


        def _persist_checkpoint(self, document: dict[str, Any]) -> None:
            identity = self._authenticated_agent(document)
            if identity["role"] == "target" and identity["cluster_id"] is None:
                raise ControlApiError("TARGET_AGENT_IDENTITY_REQUIRED")
            if identity["role"] == "eph" and identity["cluster_id"] is not None:
                raise ControlApiError("EPH_AGENT_CLUSTER_SCOPE_INVALID")
            deployment_id = _parse_uuid(document.get("deployment_id"), "DEPLOYMENT_ID")
            command_id = _parse_uuid(document.get("command_id"), "COMMAND_ID")
            lease_id = _parse_uuid(document.get("lease_id"), "LEASE_ID")
            lease_token = document.get("lease_token")
            if not isinstance(lease_token, str) or len(lease_token) < 32:
                raise ControlApiError("LEASE_TOKEN_INVALID")
            token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
            expected_phase = document.get("expected_phase")
            expected_sequence = document.get("expected_sequence")
            next_phase = document.get("phase")
            state_digest = document.get("state_digest")
            evidence = document.get("evidence")
            if not isinstance(expected_phase, str) or not expected_phase:
                raise ControlApiError("CHECKPOINT_EXPECTED_PHASE_INVALID")
            if (
                not isinstance(expected_sequence, int)
                or isinstance(expected_sequence, bool)
                or expected_sequence < 0
            ):
                raise ControlApiError("CHECKPOINT_SEQUENCE_INVALID")
            if not isinstance(next_phase, str) or not next_phase:
                raise ControlApiError("CHECKPOINT_PHASE_INVALID")
            if not isinstance(state_digest, str) or len(state_digest) != 64:
                raise ControlApiError("CHECKPOINT_DIGEST_INVALID")
            try:
                int(state_digest, 16)
            except ValueError as exc:
                raise ControlApiError("CHECKPOINT_DIGEST_INVALID") from exc
            if not isinstance(evidence, dict) or len(_json_bytes(evidence)) > MAX_BODY_BYTES:
                raise ControlApiError("CHECKPOINT_EVIDENCE_INVALID")
            if not secrets.compare_digest(
                _checkpoint_state_digest(evidence),
                state_digest.lower(),
            ):
                raise ControlApiError("CHECKPOINT_EVIDENCE_DIGEST_MISMATCH")
            _validate_checkpoint_actor_transition(
                identity["role"],
                expected_phase,
                next_phase,
            )
            state = {"state_digest": state_digest.lower(), "evidence": evidence}
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    deployment = connection.execute(
                        """
                        SELECT installation_id, cluster_id, phase, sequence, state
                        FROM agent_control_deployments
                        WHERE deployment_id = %s
                        FOR UPDATE
                        """,
                        (deployment_id,),
                    ).fetchone()
                    if (
                        deployment is None
                        or deployment["installation_id"] != identity["installation_id"]
                    ):
                        raise ControlApiError("CHECKPOINT_DEPLOYMENT_IDENTITY_MISMATCH")
                    if (
                        identity["role"] == "target"
                        and deployment["cluster_id"] != identity["cluster_id"]
                    ):
                        raise ControlApiError("CHECKPOINT_TARGET_CLUSTER_MISMATCH")
                    leased_command = connection.execute(
                        """
                        SELECT deployment_id FROM agent_control_commands
                        WHERE command_id = %s AND deployment_id = %s
                          AND target_agent_id = %s AND lease_id = %s
                          AND lease_token_hash = %s AND status = 'leased'
                          AND lease_expires_at > NOW()
                        FOR UPDATE
                        """,
                        (
                            command_id,
                            deployment_id,
                            identity["agent_id"],
                            lease_id,
                            token_hash,
                        ),
                    ).fetchone()
                    if leased_command is None:
                        raise ControlApiError("CHECKPOINT_COMMAND_LEASE_INVALID")
                    updated = connection.execute(
                        """
                        UPDATE agent_control_deployments
                        SET phase = %s, sequence = %s, state = %s, updated_at = NOW()
                        WHERE deployment_id = %s AND cluster_id = %s
                          AND phase = %s AND sequence = %s
                        RETURNING phase, sequence
                        """,
                        (
                            next_phase,
                            expected_sequence + 1,
                            Jsonb(state),
                            deployment_id,
                            deployment["cluster_id"],
                            expected_phase,
                            expected_sequence,
                        ),
                    ).fetchone()
                    if updated is None:
                        current = connection.execute(
                            """
                            SELECT phase, sequence, state
                            FROM agent_control_deployments
                            WHERE deployment_id = %s AND cluster_id = %s
                            """,
                            (deployment_id, deployment["cluster_id"]),
                        ).fetchone()
                        if (
                            current
                            and current["phase"] == next_phase
                            and current["sequence"] == expected_sequence + 1
                            and isinstance(current["state"], dict)
                            and current["state"].get("state_digest") == state_digest.lower()
                        ):
                            self._send(200, {
                                "persisted": True,
                                "already_applied": True,
                                "phase": current["phase"],
                                "sequence": current["sequence"],
                            })
                            return
                        raise ControlApiError("CHECKPOINT_COMPARE_AND_SWAP_FAILED")
            self._send(200, {
                "persisted": True,
                "already_applied": False,
                "phase": next_phase,
                "sequence": expected_sequence + 1,
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
