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

from packages.agent_contracts.orchestration import (
    EPH_ACTIONS as CONTRACT_EPH_ACTIONS,
)
from packages.agent_contracts.orchestration import (
    PHASE_ORDER,
    OrchestrationError,
    next_phase_for_action,
)
from packages.agent_contracts.orchestration import (
    TARGET_ACTIONS as CONTRACT_TARGET_ACTIONS,
)
from packages.agent_contracts.orchestration import (
    plan_next_step as _plan_next_step,
)

from .deployment_controller import (
    advance_after_command,
    advance_for_agent,
    reconcile_deployments,
)
from .workspace import (
    WorkspaceError,
    normalize_name,
    normalize_plugin,
    normalize_project,
    normalize_skill,
    normalize_task,
    normalize_work,
    normalize_work_result,
    normalize_workflow_definition,
    normalize_workflow_run,
    normalize_workspace_path,
)

MAX_BODY_BYTES = 1024 * 1024
LEASE_SECONDS = 90


class ControlApiError(ValueError):
    pass

#: Action allowlists are derived from the shared contract so the control plane
#: and the Agents cannot drift. ``packages/agent_contracts`` is copied into the
#: control-plane image for this reason (see ``Dockerfile.cloud-control-agent``).
EPH_ACTIONS = frozenset(CONTRACT_EPH_ACTIONS)
TARGET_ACTIONS = frozenset(CONTRACT_TARGET_ACTIONS)


def _validate_action_role(role: str, action: str) -> None:
    allowed = EPH_ACTIONS if role == "eph" else TARGET_ACTIONS if role == "target" else frozenset()
    if action not in allowed:
        raise ControlApiError("COMMAND_ACTION_NOT_ALLOWED_FOR_AGENT_ROLE")


def plan_deployment_command(
    deployment_id: UUID,
    phase: str,
    spec: Any = None,
    evidence: Any = None,
) -> dict[str, Any]:
    """Plan the next dispatchable command for a deployment checkpoint phase.

    The control plane owns the dispatch decision, so callers (platform API or an
    operator) never hand-pick the role/action pair. The mapping itself lives in
    the shared contract; this helper only translates contract errors into
    control-plane errors and serialises the plan.
    """

    plan_spec = {} if spec is None else spec
    plan_evidence = {} if evidence is None else evidence
    try:
        plan = _plan_next_step(deployment_id, phase, plan_spec, plan_evidence)
    except OrchestrationError as exc:
        raise ControlApiError(str(exc)) from exc
    if plan is None:
        return {
            "deployment_id": str(deployment_id),
            "phase": phase,
            "terminal": True,
            "next_step": None,
        }
    return {
        "deployment_id": str(deployment_id),
        "phase": phase,
        "terminal": False,
        "next_step": {
            "role": plan.role,
            "action": plan.action,
            "next_phase": plan.next_phase.value,
            "idempotency_key": plan.idempotency_key,
            "payload": plan.payload,
        },
    }


def _command_payload_for_claim(payload: Any, next_phase: Any) -> dict[str, Any]:
    """Expose the phase transition to the Agent inside the claimed command payload.

    Target actions such as ``handoff.evaluate`` need the exact next phase, and an
    Agent must never infer it from local state. The command row is authoritative.
    """

    result = dict(payload) if isinstance(payload, dict) else {}
    if isinstance(next_phase, str) and next_phase:
        result["next_phase"] = next_phase
    return result


def _validate_reported_capabilities(role: str, capabilities: Any) -> list[str]:
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 100
        or any(not isinstance(item, str) or not item for item in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise ControlApiError("HEARTBEAT_CAPABILITIES_INVALID")
    allowed = EPH_ACTIONS if role == "eph" else TARGET_ACTIONS if role == "target" else frozenset()
    if not set(capabilities).issubset(allowed):
        raise ControlApiError("HEARTBEAT_CAPABILITY_NOT_ALLOWED_FOR_AGENT_ROLE")
    return capabilities


CHECKPOINT_PHASES = tuple(phase.value for phase in PHASE_ORDER)


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
        # EPH owns everything up to and including target control-plane readiness.
        # Target takeover starts only after that checkpoint exists.
        target_control_plane_index = CHECKPOINT_PHASES.index("target_control_plane_ready")
        if next_index > target_control_plane_index or next_index != current_index + 1:
            raise ControlApiError("EPH_CHECKPOINT_PHASE_NOT_ALLOWED")
        return
    if role != "target":
        raise ControlApiError("CHECKPOINT_AGENT_ROLE_INVALID")
    target_agent_index = CHECKPOINT_PHASES.index("target_agent_ready")
    if next_index < target_agent_index or next_index != current_index + 1:
        raise ControlApiError("TARGET_CHECKPOINT_TRANSITION_INVALID")



def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


PLANNING_ARTIFACT_KEYS = (
    "fleet_plan",
    "hosts",
    "cluster_spec",
    "control_plane_count",
    "worker_replicas",
)


def _validate_checkpoint_command_transition(
    command: dict[str, Any],
    expected_phase: str,
    next_phase: str,
    role: str,
) -> None:
    """Bind a checkpoint to the exact leased command that owns the transition."""

    if command.get("required_role") != role:
        raise ControlApiError("CHECKPOINT_COMMAND_ROLE_MISMATCH")
    if command.get("from_phase") != expected_phase:
        raise ControlApiError("CHECKPOINT_COMMAND_FROM_PHASE_MISMATCH")
    if command.get("next_phase") != next_phase:
        raise ControlApiError("CHECKPOINT_COMMAND_NEXT_PHASE_MISMATCH")


def _merge_planning_artifacts(
    previous_state: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Carry immutable planning output across checkpoint evidence replacement.

    Checkpoints intentionally replace the evidence document. The fleet roles and
    rendered cluster inputs must survive that replacement, or the BMH/CAPI phase
    would lose the Agent's reasoning. Only a fixed allowlist is retained.
    """

    artifacts: dict[str, Any] = {}
    if isinstance(previous_state, dict):
        existing = previous_state.get("artifacts")
        if isinstance(existing, dict):
            for key in PLANNING_ARTIFACT_KEYS:
                value = existing.get(key)
                if isinstance(value, dict) or isinstance(value, list):
                    artifacts[key] = value
                elif isinstance(value, int) and not isinstance(value, bool):
                    artifacts[key] = value
    for key in PLANNING_ARTIFACT_KEYS:
        value = evidence.get(key)
        if isinstance(value, dict) or isinstance(value, list):
            artifacts[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            artifacts[key] = value
    return artifacts


def _checkpoint_state_digest(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validated_eph_pivot_attestation(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    source_identities = output.get("source_bmh_identities")
    target_identities = output.get("target_bmh_identities")
    if (
        output.get("capi_pivot_succeeded") is not True
        or output.get("source_cluster_absent") is not True
        or output.get("target_cluster_ownership_verified") is not True
        or output.get("target_controllers_ready") is not True
        or output.get("cleanup_scope_attested") is not True
        or output.get("target_observer_secret_source_uid_attested") is not True
        or not isinstance(output.get("source_cleanup_resource_count"), int)
        or isinstance(output.get("source_cleanup_resource_count"), bool)
        or output.get("source_cleanup_resource_count") < 7
        or output.get("target_source_cluster_uid_annotation")
        != output.get("source_cluster_uid")
        or not isinstance(output.get("source_cluster_uid"), str)
        or not output.get("source_cluster_uid")
        or not isinstance(output.get("target_cluster_uid"), str)
        or not output.get("target_cluster_uid")
    ):
        return None

    def normalize_inventory(identities: Any) -> list[dict[str, str]] | None:
        if not isinstance(identities, list) or not 1 <= len(identities) <= 2500:
            return None
        normalized = []
        identity_keys = set()
        hardware_ids = set()
        for item in identities:
            if not isinstance(item, dict):
                return None
            namespace = item.get("namespace")
            name = item.get("name")
            hardware_identity = item.get("hardware_identity")
            if not all(
                isinstance(value, str) and value
                for value in (namespace, name, hardware_identity)
            ):
                return None
            identity_key = (namespace, name, hardware_identity)
            if identity_key in identity_keys or hardware_identity in hardware_ids:
                return None
            identity_keys.add(identity_key)
            hardware_ids.add(hardware_identity)
            normalized.append({
                "namespace": namespace,
                "name": name,
                "hardware_identity": hardware_identity,
            })
        normalized.sort(
            key=lambda item: (
                item["namespace"],
                item["name"],
                item["hardware_identity"],
            )
        )
        return normalized

    source_normalized = normalize_inventory(source_identities)
    target_normalized = normalize_inventory(target_identities)
    if (
        source_normalized is None
        or target_normalized is None
        or source_normalized != target_normalized
    ):
        return None
    source_digest = hashlib.sha256(
        json.dumps(
            source_normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    target_digest = hashlib.sha256(
        json.dumps(
            target_normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        output.get("source_bmh_digest") != source_digest
        or output.get("target_bmh_digest") != target_digest
    ):
        return None
    return {
        "source_cluster_uid": output["source_cluster_uid"],
        "target_cluster_uid": output["target_cluster_uid"],
        "source_bmh_identities": source_normalized,
        "source_bmh_digest": source_digest,
        "target_bmh_digest": target_digest,
        "cleanup_scope_attested": True,
        "source_cleanup_resource_count": output["source_cleanup_resource_count"],
        "target_observer_secret_source_uid_attested": True,
    }


def _validate_checkpoint_evidence(
    role: str,
    next_phase: str,
    evidence: Any,
) -> None:
    """Reject a checkpoint that claims a security-sensitive transition.

    A Target Agent reports the pivot attestation in the same evidence document
    it sends with the checkpoint. Validating it here prevents a forged or
    incomplete pivot from advancing the deployment before result validation.
    """

    if role == "target" and next_phase == "capi_pivot_verified":
        if not isinstance(evidence, dict) or _validated_eph_pivot_attestation(evidence) is None:
            raise ControlApiError("EPH_PIVOT_ATTESTATION_INVALID")


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


def _task_max_attempts(payload: Any) -> int:
    """Read a step retry budget, defaulting to a single attempt."""

    if not isinstance(payload, dict):
        return 1
    step = payload.get("step")
    if not isinstance(step, dict):
        return 1
    value = step.get("max_attempts", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return max(1, min(value, 5))


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
                if self.path.startswith("/api/v1/control/projects/"):
                    raw_id, collection = normalize_workspace_path(self.path)
                    project_id = _parse_uuid(raw_id, "PROJECT_ID")
                    if collection is None:
                        self._get_project(raw_id)
                    elif collection == "skills":
                        self._list_skills(project_id)
                    elif collection == "plugins":
                        self._list_plugins(project_id)
                    elif collection == "work":
                        self._list_work(project_id)
                    elif collection == "tasks":
                        self._list_tasks(project_id)
                    else:
                        self._list_workflows(project_id)
                    return
                if self.path.startswith("/api/v1/control/workflows/"):
                    self._get_workflow(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/workflow-runs/"):
                    self._get_workflow_run(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/tasks/"):
                    self._get_task(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/work/"):
                    self._get_work(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/deployments/"):
                    self._get_deployment(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/commands/"):
                    self._get_command(self.path.rsplit("/", 1)[-1])
                    return
                if self.path.startswith("/api/v1/control/agents/"):
                    self._get_agent(self.path.rsplit("/", 1)[-1])
                    return
            except (ControlApiError, WorkspaceError) as exc:
                self._send(400, {"error": str(exc)})
                return
            except psycopg.Error:
                self._send(503, {"error": "PERSISTENCE_UNAVAILABLE"})
                return
            self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                document = self._body()
                if self.path == "/api/v1/agent-control/tasks/claim":
                    self._claim_task(document)
                    return
                if self.path == "/api/v1/agent-control/tasks/lease/renew":
                    self._renew_task_lease(document)
                    return
                if self.path == "/api/v1/agent-control/tasks/result":
                    self._task_result(document)
                    return
                if self.path == "/api/v1/agent-control/work/claim":
                    self._claim_work(document)
                    return
                if self.path == "/api/v1/agent-control/work/lease/renew":
                    self._renew_work_lease(document)
                    return
                if self.path == "/api/v1/agent-control/work/result":
                    self._work_result(document)
                    return
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
                if self.path == "/api/v1/control/projects":
                    self._create_project(document)
                    return
                if self.path.startswith("/api/v1/control/projects/"):
                    raw_id, collection = normalize_workspace_path(self.path)
                    project_id = _parse_uuid(raw_id, "PROJECT_ID")
                    if collection == "skills":
                        self._create_skill(project_id, document)
                    elif collection == "plugins":
                        self._create_plugin(project_id, document)
                    elif collection == "work":
                        self._create_work(project_id, document)
                    elif collection == "tasks":
                        self._create_task(project_id, document)
                    elif collection == "workflows":
                        self._create_workflow(project_id, document)
                    else:
                        raise ControlApiError("PROJECT_COLLECTION_REQUIRED")
                    return
                if self.path.startswith("/api/v1/control/workflows/"):
                    parts = self.path.strip("/").split("/")
                    if len(parts) != 6 or parts[3] != "workflows":
                        raise ControlApiError("WORKFLOW_ACTION_INVALID")
                    workflow_id = _parse_uuid(parts[4], "WORKFLOW_ID")
                    if parts[5] == "runs":
                        self._start_workflow_run(workflow_id, document)
                    elif parts[5] == "versions":
                        self._create_workflow_version(workflow_id, document)
                    else:
                        raise ControlApiError("WORKFLOW_ACTION_INVALID")
                    return
                if self.path.startswith("/api/v1/control/workflow-runs/"):
                    if self.path.endswith("/cancel"):
                        run_raw = self.path.rsplit("/", 2)[-2]
                        self._cancel_workflow_run(run_raw)
                        return
                    raise ControlApiError("WORKFLOW_RUN_ACTION_INVALID")
                if self.path.startswith("/api/v1/control/work/"):
                    if self.path.endswith("/cancel"):
                        work_raw = self.path.rsplit("/", 2)[-2]
                        self._cancel_work(work_raw, document)
                        return
                    raise ControlApiError("WORK_ACTION_INVALID")
                if self.path == "/api/v1/control/deployments":
                    self._create_deployment(document)
                    return
                if self.path == "/api/v1/control/deployments/plan":
                    self._plan_deployment(document)
                    return
                if self.path == "/api/v1/control/deployments/reconcile":
                    self._reconcile_deployments(document)
                    return
                if self.path == "/api/v1/control/commands":
                    self._enqueue(document)
                    return
                self._send(404, {"error": "NOT_FOUND"})
            except (ControlApiError, WorkspaceError) as exc:
                self._send(400, {"error": str(exc)})
            except psycopg.Error:
                self._send(503, {"error": "PERSISTENCE_UNAVAILABLE"})



        def _require_platform_api(self) -> None:
            if _certificate_common_name(self.connection.getpeercert()) != "platform-api":
                raise ControlApiError("PLATFORM_API_CERTIFICATE_REQUIRED")

        def _project_row(self, project_id: UUID) -> dict[str, Any]:
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT project_id, installation_id, name, description, status,
                           metadata, created_at, updated_at
                    FROM agent_control_projects WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("PROJECT_NOT_FOUND")
            return row

        def _create_project(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project_id = _parse_uuid(document.get("project_id"), "PROJECT_ID")
            installation_id = _parse_uuid(document.get("installation_id"), "INSTALLATION_ID")
            try:
                project = normalize_project(
                    name=document.get("name"),
                    description=document.get("description", ""),
                    metadata=document.get("metadata"),
                    status=document.get("status", "active"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO agent_control_projects (
                            project_id, installation_id, name, description, status, metadata
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id) DO NOTHING
                        """,
                        (
                            project_id, installation_id, project["name"],
                            project["description"], project["status"],
                            Jsonb(project["metadata"]),
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("PROJECT_NAME_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT project_id, installation_id, name, description, status,
                           metadata, created_at, updated_at
                    FROM agent_control_projects WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
            if (
                row is None
                or row["installation_id"] != installation_id
                or row["name"] != project["name"]
            ):
                raise ControlApiError("PROJECT_IDENTITY_CONFLICT")
            self._send(200, {"created": True, **row})

        def _get_project(self, raw_id: str) -> None:
            self._require_platform_api()
            project_id = _parse_uuid(raw_id, "PROJECT_ID")
            row = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM agent_control_skills
                         WHERE project_id = %(project_id)s) AS skill_count,
                        (SELECT COUNT(*) FROM agent_control_plugins
                         WHERE project_id = %(project_id)s) AS plugin_count,
                        (SELECT COUNT(*) FROM agent_control_work_items
                         WHERE project_id = %(project_id)s AND status = 'queued') AS queued_work
                    """,
                    {"project_id": project_id},
                ).fetchone()
            self._send(200, {**row, **(counts or {})})

        def _create_skill(self, project_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            if project["status"] != "active":
                raise ControlApiError("PROJECT_NOT_ACTIVE")
            skill_id = _parse_uuid(document.get("skill_id"), "SKILL_ID")
            try:
                skill = normalize_skill(
                    name=document.get("name"),
                    version=document.get("version"),
                    description=document.get("description", ""),
                    manifest=document.get("manifest"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO agent_control_skills (
                            skill_id, project_id, name, version, description, manifest
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            skill_id, project_id, skill["name"], skill["version"],
                            skill["description"], Jsonb(skill["manifest"]),
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("SKILL_VERSION_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT skill_id, project_id, name, version, description,
                           manifest, enabled, created_at
                    FROM agent_control_skills WHERE skill_id = %s
                    """,
                    (skill_id,),
                ).fetchone()
            self._send(200, {"created": True, **row})

        def _list_skills(self, project_id: UUID) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT skill_id, name, version, description, manifest, enabled,
                           created_at, updated_at
                    FROM agent_control_skills WHERE project_id = %s
                    ORDER BY name, version
                    """,
                    (project_id,),
                ).fetchall()
            self._send(200, {"project_id": project["project_id"], "results": rows})

        def _create_plugin(self, project_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            if project["status"] != "active":
                raise ControlApiError("PROJECT_NOT_ACTIVE")
            plugin_id = _parse_uuid(document.get("plugin_id"), "PLUGIN_ID")
            try:
                plugin = normalize_plugin(
                    name=document.get("name"),
                    version=document.get("version"),
                    kind=document.get("kind"),
                    description=document.get("description", ""),
                    config_ref=document.get("config_ref"),
                    manifest=document.get("manifest"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO agent_control_plugins (
                            plugin_id, project_id, name, version, kind,
                            description, config_ref, manifest
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            plugin_id, project_id, plugin["name"], plugin["version"],
                            plugin["kind"], plugin["description"],
                            plugin["config_ref"], Jsonb(plugin["manifest"]),
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("PLUGIN_VERSION_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT plugin_id, project_id, name, version, kind, description,
                           config_ref, manifest, enabled, created_at
                    FROM agent_control_plugins WHERE plugin_id = %s
                    """,
                    (plugin_id,),
                ).fetchone()
            self._send(200, {"created": True, **row})

        def _list_plugins(self, project_id: UUID) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT plugin_id, name, version, kind, description, config_ref,
                           manifest, enabled, created_at, updated_at
                    FROM agent_control_plugins WHERE project_id = %s
                    ORDER BY name, version
                    """,
                    (project_id,),
                ).fetchall()
            self._send(200, {"project_id": project["project_id"], "results": rows})

        def _create_work(self, project_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            if project["status"] != "active":
                raise ControlApiError("PROJECT_NOT_ACTIVE")
            work_id = _parse_uuid(document.get("work_id"), "WORK_ID")
            try:
                work = normalize_work(
                    kind=document.get("kind"),
                    payload=document.get("payload"),
                    required_role=document.get("required_role"),
                    priority=document.get("priority", 0),
                    idempotency_key=document.get("idempotency_key"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO agent_control_work_items (
                            work_id, project_id, kind, priority, required_role,
                            payload, idempotency_key
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            work_id, project_id, work["kind"], work["priority"],
                            work["required_role"], Jsonb(work["payload"]),
                            work["idempotency_key"],
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("WORK_ID_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT work_id, project_id, kind, status, priority, required_role,
                           payload, result, idempotency_key, assigned_agent_id,
                           attempts, created_at, updated_at
                    FROM agent_control_work_items WHERE work_id = %s
                    """,
                    (work_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT work_id, project_id, kind, status, priority, required_role,
                               payload, result, idempotency_key, assigned_agent_id,
                               attempts, created_at, updated_at
                        FROM agent_control_work_items WHERE idempotency_key = %s
                        """,
                        (work["idempotency_key"],),
                    ).fetchone()
            if row is None:
                raise ControlApiError("WORK_NOT_PERSISTED")
            if (
                row["project_id"] != project_id
                or row["kind"] != work["kind"]
                or row["payload"] != work["payload"]
            ):
                raise ControlApiError("WORK_IDEMPOTENCY_CONFLICT")
            self._send(200, {"created": row["work_id"] == work_id, **row})

        def _list_work(self, project_id: UUID) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT work_id, kind, status, priority, required_role, payload,
                           result, assigned_agent_id, attempts, created_at, updated_at
                    FROM agent_control_work_items WHERE project_id = %s
                    ORDER BY created_at DESC
                    """,
                    (project_id,),
                ).fetchall()
            self._send(200, {"project_id": project["project_id"], "results": rows})

        def _get_work(self, raw_id: str) -> None:
            self._require_platform_api()
            work_id = _parse_uuid(raw_id, "WORK_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT work_id, project_id, kind, status, priority, required_role,
                           payload, result, assigned_agent_id, attempts,
                           lease_expires_at, created_at, updated_at
                    FROM agent_control_work_items WHERE work_id = %s
                    """,
                    (work_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("WORK_NOT_FOUND")
            self._send(200, row)

        def _claim_work(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            lease_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
            row = None
            skills: list[dict[str, Any]] = []
            plugins: list[dict[str, Any]] = []
            with psycopg.connect(
                database_url, row_factory=dict_row, autocommit=False
            ) as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT w.work_id, w.project_id, w.kind, w.priority,
                               w.required_role, w.payload, p.name AS project_name
                        FROM agent_control_work_items w
                        JOIN agent_control_projects p ON p.project_id = w.project_id
                        WHERE p.installation_id = %(installation_id)s
                          AND p.status = 'active'
                          AND (w.status = 'queued'
                               OR (w.status = 'leased' AND w.lease_expires_at < NOW()))
                          AND (w.required_role IS NULL OR w.required_role = %(role)s)
                        ORDER BY w.priority DESC, w.created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        {"installation_id": agent["installation_id"], "role": agent["role"]},
                    ).fetchone()
                    if row is not None:
                        connection.execute(
                            """
                            UPDATE agent_control_work_items
                            SET status = 'leased', assigned_agent_id = %s,
                                lease_token_hash = %s,
                                lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                                attempts = attempts + 1, updated_at = NOW()
                            WHERE work_id = %s
                            """,
                            (agent["agent_id"], token_hash, LEASE_SECONDS, row["work_id"]),
                        )
                        skills = connection.execute(
                            """
                            SELECT skill_id, name, version, description, manifest
                            FROM agent_control_skills
                            WHERE project_id = %s AND enabled = TRUE ORDER BY name, version
                            """,
                            (row["project_id"],),
                        ).fetchall()
                        plugins = connection.execute(
                            """
                            SELECT plugin_id, name, version, kind, description, config_ref,
                                   manifest
                            FROM agent_control_plugins
                            WHERE project_id = %s AND enabled = TRUE ORDER BY name, version
                            """,
                            (row["project_id"],),
                        ).fetchall()
            if row is None:
                self._send(200, {"work": None})
                return
            self._send(200, {
                "work": {
                    "work_id": row["work_id"],
                    "project_id": row["project_id"],
                    "project_name": row["project_name"],
                    "kind": row["kind"],
                    "priority": row["priority"],
                    "payload": row["payload"],
                },
                "skills": skills,
                "plugins": plugins,
                "lease_token": lease_token,
                "lease_seconds": LEASE_SECONDS,
            })

        def _renew_work_lease(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            work_id = _parse_uuid(document.get("work_id"), "WORK_ID")
            token = document.get("lease_token")
            if not isinstance(token, str) or not token:
                raise ControlApiError("WORK_LEASE_TOKEN_REQUIRED")
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    UPDATE agent_control_work_items
                    SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE work_id = %s AND assigned_agent_id = %s
                      AND status = 'leased' AND lease_token_hash = %s
                    RETURNING lease_expires_at
                    """,
                    (LEASE_SECONDS, work_id, agent["agent_id"], token_hash),
                ).fetchone()
            if row is None:
                raise ControlApiError("WORK_LEASE_INVALID")
            self._send(200, {"renewed": True, **row})

        def _work_result(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            work_id = _parse_uuid(document.get("work_id"), "WORK_ID")
            try:
                outcome = normalize_work_result(
                    status=document.get("status"),
                    result=document.get("result"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    UPDATE agent_control_work_items
                    SET status = %s, result = %s, lease_token_hash = NULL,
                        lease_expires_at = NULL, updated_at = NOW()
                    WHERE work_id = %s AND assigned_agent_id = %s
                      AND status = 'leased'
                    RETURNING work_id, status, result, updated_at
                    """,
                    (outcome["status"], Jsonb(outcome["result"]), work_id, agent["agent_id"]),
                ).fetchone()
            if row is None:
                raise ControlApiError("WORK_RESULT_NOT_ACCEPTED")
            self._send(200, {"accepted": True, **row})

        def _cancel_work(self, raw_id: str, document: dict[str, Any]) -> None:
            self._require_platform_api()
            work_id = _parse_uuid(raw_id, "WORK_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    UPDATE agent_control_work_items
                    SET status = 'cancelled', lease_token_hash = NULL,
                        lease_expires_at = NULL, updated_at = NOW()
                    WHERE work_id = %s AND status IN ('queued', 'leased')
                    RETURNING work_id, status
                    """,
                    (work_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("WORK_NOT_CANCELLABLE")
            self._send(200, {"cancelled": True, **row})

        def _create_workflow(self, project_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            if project["status"] != "active":
                raise ControlApiError("PROJECT_NOT_ACTIVE")
            workflow_id = _parse_uuid(document.get("workflow_id"), "WORKFLOW_ID")
            try:
                name = normalize_name(document.get("name"), "workflow name")
                definition = normalize_workflow_definition(document.get("definition"))
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO agent_control_workflows (
                                workflow_id, project_id, name, description
                            ) VALUES (%s, %s, %s, %s)
                            """,
                            (workflow_id, project_id, name, definition["description"]),
                        )
                        connection.execute(
                            """
                            INSERT INTO agent_control_workflow_versions (
                                workflow_id, version, definition
                            ) VALUES (%s, 1, %s)
                            """,
                            (workflow_id, Jsonb(definition)),
                        )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("WORKFLOW_NAME_OR_ID_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT workflow_id, project_id, name, description, status,
                           latest_version, created_at
                    FROM agent_control_workflows WHERE workflow_id = %s
                    """,
                    (workflow_id,),
                ).fetchone()
            self._send(200, {"created": True, **row, "definition": definition})

        def _list_workflows(self, project_id: UUID) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT w.workflow_id, w.name, w.description, w.status,
                           w.latest_version, w.created_at, w.updated_at,
                           COALESCE(jsonb_array_length(v.definition->'steps'), 0)
                               AS step_count
                    FROM agent_control_workflows w
                    LEFT JOIN agent_control_workflow_versions v
                      ON v.workflow_id = w.workflow_id AND v.version = w.latest_version
                    WHERE w.project_id = %s ORDER BY w.name
                    """,
                    (project_id,),
                ).fetchall()
            self._send(200, {"project_id": project["project_id"], "results": rows})

        def _get_workflow(self, raw_id: str) -> None:
            self._require_platform_api()
            workflow_id = _parse_uuid(raw_id, "WORKFLOW_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT w.workflow_id, w.project_id, w.name, w.description,
                           w.status, w.latest_version, w.created_at, w.updated_at,
                           v.definition
                    FROM agent_control_workflows w
                    JOIN agent_control_workflow_versions v
                      ON v.workflow_id = w.workflow_id AND v.version = w.latest_version
                    WHERE w.workflow_id = %s
                    """,
                    (workflow_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("WORKFLOW_NOT_FOUND")
            self._send(200, row)

        def _create_workflow_version(self, workflow_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            try:
                definition = normalize_workflow_definition(document.get("definition"))
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    workflow = connection.execute(
                        """
                        SELECT workflow_id, status, latest_version
                        FROM agent_control_workflows WHERE workflow_id = %s
                        FOR UPDATE
                        """,
                        (workflow_id,),
                    ).fetchone()
                    if workflow is None:
                        raise ControlApiError("WORKFLOW_NOT_FOUND")
                    if workflow["status"] != "published":
                        raise ControlApiError("WORKFLOW_NOT_PUBLISHED")
                    version = workflow["latest_version"] + 1
                    connection.execute(
                        """
                        INSERT INTO agent_control_workflow_versions (
                            workflow_id, version, definition
                        ) VALUES (%s, %s, %s)
                        """,
                        (workflow_id, version, Jsonb(definition)),
                    )
                    connection.execute(
                        """
                        UPDATE agent_control_workflows
                        SET latest_version = %s, description = %s, updated_at = NOW()
                        WHERE workflow_id = %s
                        """,
                        (version, definition["description"], workflow_id),
                    )
            self._send(200, {
                "created": True,
                "workflow_id": workflow_id,
                "version": version,
                "definition": definition,
            })

        def _start_workflow_run(self, workflow_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            run_id = _parse_uuid(document.get("run_id"), "RUN_ID")
            try:
                run = normalize_workflow_run(
                    input=document.get("input"),
                    idempotency_key=document.get("idempotency_key"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    workflow = connection.execute(
                        """
                        SELECT workflow_id, project_id, status, latest_version
                        FROM agent_control_workflows WHERE workflow_id = %s
                        """,
                        (workflow_id,),
                    ).fetchone()
                    if workflow is None:
                        raise ControlApiError("WORKFLOW_NOT_FOUND")
                    if workflow["status"] != "published":
                        raise ControlApiError("WORKFLOW_NOT_PUBLISHED")
                    project = connection.execute(
                        """
                        SELECT status FROM agent_control_projects WHERE project_id = %s
                        """,
                        (workflow["project_id"],),
                    ).fetchone()
                    if project is None or project["status"] != "active":
                        raise ControlApiError("PROJECT_NOT_ACTIVE")
                    version = workflow["latest_version"]
                    definition_row = connection.execute(
                        """
                        SELECT definition FROM agent_control_workflow_versions
                        WHERE workflow_id = %s AND version = %s
                        """,
                        (workflow_id, version),
                    ).fetchone()
                    if definition_row is None:
                        raise ControlApiError("WORKFLOW_VERSION_NOT_FOUND")
                    try:
                        inserted = connection.execute(
                            """
                            INSERT INTO agent_control_workflow_runs (
                                run_id, workflow_id, project_id, version,
                                input, idempotency_key
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (run_id) DO NOTHING
                            RETURNING run_id
                            """,
                            (
                                run_id, workflow_id, workflow["project_id"], version,
                                Jsonb(run["input"]), run["idempotency_key"],
                            ),
                        ).fetchone()
                    except psycopg.errors.UniqueViolation as exc:
                        raise ControlApiError("WORKFLOW_RUN_IDEMPOTENCY_CONFLICT") from exc
                    if inserted is not None:
                        for step in definition_row["definition"]["steps"]:
                            connection.execute(
                                """
                                INSERT INTO agent_control_tasks (
                                    task_id, project_id, workflow_run_id, step_id,
                                    name, status, required_role, payload, depends_on,
                                    idempotency_key
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    uuid4(),
                                    workflow["project_id"],
                                    run_id,
                                    step["id"],
                                    step["name"],
                                    "queued" if not step["depends_on"] else "blocked",
                                    step["required_role"],
                                    Jsonb({
                                        "workflow_input": run["input"],
                                        "step": step["payload"],
                                    }),
                                    Jsonb(step["depends_on"]),
                                    f"workflow:{run_id}:{step['id']}",
                                ),
                            )
                    else:
                        existing = connection.execute(
                            """
                            SELECT project_id, idempotency_key FROM agent_control_workflow_runs
                            WHERE run_id = %s
                            """,
                            (run_id,),
                        ).fetchone()
                        if (
                            existing is None
                            or existing["project_id"] != workflow["project_id"]
                            or existing["idempotency_key"] != run["idempotency_key"]
                        ):
                            raise ControlApiError("WORKFLOW_RUN_IDENTITY_CONFLICT")
                    run_row = connection.execute(
                        """
                        SELECT run_id, workflow_id, project_id, version, status,
                               input, output, idempotency_key, started_at, finished_at
                        FROM agent_control_workflow_runs WHERE run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                    tasks = connection.execute(
                        """
                        SELECT task_id, step_id, name, status, priority, required_role,
                               payload, depends_on, result, assigned_agent_id, attempts
                        FROM agent_control_tasks WHERE workflow_run_id = %s
                        ORDER BY created_at, step_id
                        """,
                        (run_id,),
                    ).fetchall()
                self._send(200, {"created": inserted is not None, "run": run_row, "tasks": tasks})

        def _get_workflow_run(self, raw_id: str) -> None:
            self._require_platform_api()
            run_id = _parse_uuid(raw_id, "RUN_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                run = connection.execute(
                    """
                    SELECT r.run_id, r.workflow_id, w.name AS workflow_name,
                           r.project_id, r.version, r.status, r.input, r.output,
                           r.idempotency_key, r.started_at, r.finished_at
                    FROM agent_control_workflow_runs r
                    JOIN agent_control_workflows w ON w.workflow_id = r.workflow_id
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise ControlApiError("WORKFLOW_RUN_NOT_FOUND")
                tasks = connection.execute(
                    """
                    SELECT task_id, step_id, name, status, priority, required_role,
                           payload, depends_on, result, assigned_agent_id,
                           attempts, created_at, updated_at
                    FROM agent_control_tasks WHERE workflow_run_id = %s
                    ORDER BY created_at, step_id
                    """,
                    (run_id,),
                ).fetchall()
            self._send(200, {"run": run, "tasks": tasks})

        def _cancel_workflow_run(self, raw_id: str) -> None:
            self._require_platform_api()
            run_id = _parse_uuid(raw_id, "RUN_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    run = connection.execute(
                        """
                        UPDATE agent_control_workflow_runs
                        SET status = 'cancelled', finished_at = NOW(), updated_at = NOW()
                        WHERE run_id = %s AND status = 'running'
                        RETURNING run_id, status
                        """,
                        (run_id,),
                    ).fetchone()
                    if run is None:
                        raise ControlApiError("WORKFLOW_RUN_NOT_CANCELLABLE")
                    connection.execute(
                        """
                        UPDATE agent_control_tasks
                        SET status = 'cancelled', lease_token_hash = NULL,
                            lease_expires_at = NULL, updated_at = NOW()
                        WHERE workflow_run_id = %s
                          AND status IN ('blocked', 'queued', 'leased')
                        """,
                        (run_id,),
                    )
            self._send(200, {"cancelled": True, **run})

        def _create_task(self, project_id: UUID, document: dict[str, Any]) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            if project["status"] != "active":
                raise ControlApiError("PROJECT_NOT_ACTIVE")
            task_id = _parse_uuid(document.get("task_id"), "TASK_ID")
            try:
                task = normalize_task(
                    name=document.get("name"),
                    payload=document.get("payload"),
                    required_role=document.get("required_role"),
                    priority=document.get("priority", 0),
                    idempotency_key=document.get("idempotency_key"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO agent_control_tasks (
                            task_id, project_id, name, priority, required_role,
                            payload, idempotency_key
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            task_id, project_id, task["name"], task["priority"],
                            task["required_role"], Jsonb(task["payload"]),
                            task["idempotency_key"],
                        ),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise ControlApiError("TASK_ID_CONFLICT") from exc
                row = connection.execute(
                    """
                    SELECT task_id, project_id, name, status, priority, required_role,
                           payload, result, idempotency_key, attempts, created_at
                    FROM agent_control_tasks WHERE task_id = %s
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT task_id, project_id, name, status, priority, required_role,
                               payload, result, idempotency_key, attempts, created_at
                        FROM agent_control_tasks WHERE idempotency_key = %s
                        """,
                        (task["idempotency_key"],),
                    ).fetchone()
            if (
                row is None
                or row["project_id"] != project_id
                or row["payload"] != task["payload"]
            ):
                raise ControlApiError("TASK_IDEMPOTENCY_CONFLICT")
            self._send(200, {"created": row["task_id"] == task_id, **row})

        def _list_tasks(self, project_id: UUID) -> None:
            self._require_platform_api()
            project = self._project_row(project_id)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT task_id, workflow_run_id, step_id, name, status, priority,
                           required_role, payload, depends_on, result,
                           assigned_agent_id, attempts, created_at, updated_at
                    FROM agent_control_tasks WHERE project_id = %s
                    ORDER BY created_at DESC
                    """,
                    (project_id,),
                ).fetchall()
            self._send(200, {"project_id": project["project_id"], "results": rows})

        def _get_task(self, raw_id: str) -> None:
            self._require_platform_api()
            task_id = _parse_uuid(raw_id, "TASK_ID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    SELECT task_id, project_id, workflow_run_id, step_id, name, status,
                           priority, required_role, payload, depends_on, result,
                           assigned_agent_id, attempts, lease_expires_at,
                           created_at, updated_at
                    FROM agent_control_tasks WHERE task_id = %s
                    """,
                    (task_id,),
                ).fetchone()
            if row is None:
                raise ControlApiError("TASK_NOT_FOUND")
            self._send(200, row)

        def _claim_task(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            lease_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
            row = None
            skills: list[dict[str, Any]] = []
            plugins: list[dict[str, Any]] = []
            with psycopg.connect(
                database_url, row_factory=dict_row, autocommit=False
            ) as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT t.task_id, t.project_id, t.workflow_run_id, t.step_id,
                               t.name, t.priority, t.required_role, t.payload,
                               p.name AS project_name, r.workflow_id, r.status AS run_status,
                               w.name AS workflow_name
                        FROM agent_control_tasks t
                        JOIN agent_control_projects p ON p.project_id = t.project_id
                        LEFT JOIN agent_control_workflow_runs r ON r.run_id = t.workflow_run_id
                        LEFT JOIN agent_control_workflows w ON w.workflow_id = r.workflow_id
                        WHERE p.installation_id = %(installation_id)s
                          AND p.status = 'active'
                          AND (t.status = 'queued'
                               OR (t.status = 'leased' AND t.lease_expires_at < NOW()))
                          AND (t.workflow_run_id IS NULL OR r.status = 'running')
                          AND (t.required_role IS NULL OR t.required_role = %(role)s)
                        ORDER BY t.priority DESC, t.created_at
                        FOR UPDATE OF t SKIP LOCKED
                        LIMIT 1
                        """,
                        {"installation_id": agent["installation_id"], "role": agent["role"]},
                    ).fetchone()
                    if row is not None:
                        connection.execute(
                            """
                            UPDATE agent_control_tasks
                            SET status = 'leased', assigned_agent_id = %s,
                                lease_token_hash = %s,
                                lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                                attempts = attempts + 1, updated_at = NOW()
                            WHERE task_id = %s
                            """,
                            (agent["agent_id"], token_hash, LEASE_SECONDS, row["task_id"]),
                        )
                        skills = connection.execute(
                            """
                            SELECT skill_id, name, version, description, manifest
                            FROM agent_control_skills
                            WHERE project_id = %s AND enabled = TRUE ORDER BY name, version
                            """,
                            (row["project_id"],),
                        ).fetchall()
                        plugins = connection.execute(
                            """
                            SELECT plugin_id, name, version, kind, description, config_ref,
                                   manifest
                            FROM agent_control_plugins
                            WHERE project_id = %s AND enabled = TRUE ORDER BY name, version
                            """,
                            (row["project_id"],),
                        ).fetchall()
            if row is None:
                self._send(200, {"task": None})
                return
            self._send(200, {
                "task": {
                    "task_id": row["task_id"],
                    "project_id": row["project_id"],
                    "project_name": row["project_name"],
                    "workflow_run_id": row["workflow_run_id"],
                    "workflow_id": row["workflow_id"],
                    "workflow_name": row["workflow_name"],
                    "step_id": row["step_id"],
                    "name": row["name"],
                    "priority": row["priority"],
                    "payload": row["payload"],
                },
                "skills": skills,
                "plugins": plugins,
                "lease_token": lease_token,
                "lease_seconds": LEASE_SECONDS,
            })

        def _renew_task_lease(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            task_id = _parse_uuid(document.get("task_id"), "TASK_ID")
            token = document.get("lease_token")
            if not isinstance(token, str) or not token:
                raise ControlApiError("TASK_LEASE_TOKEN_REQUIRED")
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """
                    UPDATE agent_control_tasks
                    SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE task_id = %s AND assigned_agent_id = %s
                      AND status = 'leased' AND lease_token_hash = %s
                    RETURNING lease_expires_at
                    """,
                    (LEASE_SECONDS, task_id, agent["agent_id"], token_hash),
                ).fetchone()
            if row is None:
                raise ControlApiError("TASK_LEASE_INVALID")
            self._send(200, {"renewed": True, **row})

        def _task_result(self, document: dict[str, Any]) -> None:
            agent = self._authenticated_agent(document)
            task_id = _parse_uuid(document.get("task_id"), "TASK_ID")
            token = document.get("lease_token")
            if not isinstance(token, str) or not token:
                raise ControlApiError("TASK_LEASE_TOKEN_REQUIRED")
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            try:
                outcome = normalize_work_result(
                    status=document.get("status"),
                    result=document.get("result"),
                )
            except WorkspaceError as exc:
                raise ControlApiError(str(exc)) from exc
            run_status = None
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    task = connection.execute(
                        """
                        UPDATE agent_control_tasks
                        SET status = %s, result = %s, lease_token_hash = NULL,
                            lease_expires_at = NULL, updated_at = NOW()
                        WHERE task_id = %s AND assigned_agent_id = %s
                          AND status = 'leased' AND lease_token_hash = %s
                        RETURNING task_id, workflow_run_id, step_id, status, result,
                                  payload, attempts
                        """,
                        (
                            outcome["status"], Jsonb(outcome["result"]), task_id,
                            agent["agent_id"], token_hash,
                        ),
                    ).fetchone()
                    if task is None:
                        raise ControlApiError("TASK_RESULT_NOT_ACCEPTED")
                    run_id = task["workflow_run_id"]
                    retrying = False
                    if (
                        run_id is not None
                        and task["status"] == "failed"
                        and task["attempts"] < _task_max_attempts(task["payload"])
                    ):
                        connection.execute(
                            """
                            UPDATE agent_control_tasks
                            SET status = 'queued', result = NULL,
                                lease_token_hash = NULL, lease_expires_at = NULL,
                                updated_at = NOW()
                            WHERE task_id = %s
                            """,
                            (task["task_id"],),
                        )
                        retrying = True
                        run_status = "running"
                    elif run_id is not None and task["status"] == "failed":
                        connection.execute(
                            """
                            UPDATE agent_control_workflow_runs
                            SET status = 'failed', output = %s,
                                finished_at = NOW(), updated_at = NOW()
                            WHERE run_id = %s AND status = 'running'
                            """,
                            (
                                Jsonb({
                                    "failed_step": task["step_id"],
                                    "error": outcome["result"],
                                }),
                                run_id,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE agent_control_tasks
                            SET status = 'cancelled', lease_token_hash = NULL,
                                lease_expires_at = NULL, updated_at = NOW()
                            WHERE workflow_run_id = %s
                              AND status IN ('blocked', 'queued', 'leased')
                            """,
                            (run_id,),
                        )
                        run_status = "failed"
                    elif run_id is not None:
                        connection.execute(
                            """
                            UPDATE agent_control_tasks t
                            SET status = 'queued', updated_at = NOW()
                            WHERE t.workflow_run_id = %s AND t.status = 'blocked'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM jsonb_array_elements_text(t.depends_on) AS dep(value)
                                  LEFT JOIN agent_control_tasks d
                                    ON d.workflow_run_id = t.workflow_run_id
                                   AND d.step_id = dep.value
                                  WHERE d.task_id IS NULL OR d.status <> 'succeeded'
                              )
                            """,
                            (run_id,),
                        )
                        active = connection.execute(
                            """
                            SELECT COUNT(*) AS active FROM agent_control_tasks
                            WHERE workflow_run_id = %s
                              AND status IN ('blocked', 'queued', 'leased')
                            """,
                            (run_id,),
                        ).fetchone()
                        if active and active["active"] == 0:
                            output = connection.execute(
                                """
                                SELECT COALESCE(jsonb_object_agg(step_id, result), '{}'::jsonb)
                                    AS output
                                FROM agent_control_tasks
                                WHERE workflow_run_id = %s AND status = 'succeeded'
                                  AND step_id IS NOT NULL
                                """,
                                (run_id,),
                            ).fetchone()
                            connection.execute(
                                """
                                UPDATE agent_control_workflow_runs
                                SET status = 'succeeded', output = %s,
                                    finished_at = NOW(), updated_at = NOW()
                                WHERE run_id = %s AND status = 'running'
                                """,
                                (Jsonb(output["output"] if output else {}), run_id),
                            )
                            run_status = "succeeded"
                        else:
                            run_status = "running"
            self._send(200, {
                "accepted": True,
                "task_id": task["task_id"],
                "status": task["status"],
                "workflow_run_id": task["workflow_run_id"],
                "workflow_run_status": run_status,
                "retrying": retrying,
            })

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
            try:
                with psycopg.connect(database_url, row_factory=dict_row) as connection:
                    advance_for_agent(connection, installation_id, cluster_id)
            except psycopg.Error:
                # Registration itself succeeded; a transient retry failure must
                # not force the Agent to redo identity registration.
                pass
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
            spec = document.get("spec", {})
            if not isinstance(spec, dict) or len(_json_bytes(spec)) > MAX_BODY_BYTES:
                raise ControlApiError("DEPLOYMENT_SPEC_INVALID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO agent_control_deployments (
                            deployment_id, installation_id, cluster_id,
                            phase, sequence, state, spec
                        )
                        VALUES (%s, %s, %s, 'not_started', 0, '{}'::jsonb, %s)
                        ON CONFLICT (deployment_id) DO NOTHING
                        """,
                        (deployment_id, installation_id, cluster_id, Jsonb(spec)),
                    )
                    row = connection.execute(
                        """
                        SELECT installation_id, cluster_id, phase, sequence, state, spec
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
                    if row["spec"] != spec:
                        raise ControlApiError("DEPLOYMENT_SPEC_CONFLICT")
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
                           created_at, updated_at
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

        def _plan_deployment(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            deployment_id = _parse_uuid(document.get("deployment_id"), "DEPLOYMENT_ID")
            spec = document.get("spec", {})
            evidence = document.get("evidence", {})
            if not isinstance(spec, dict) or len(_json_bytes(spec)) > MAX_BODY_BYTES:
                raise ControlApiError("COMMAND_PAYLOAD_INVALID")
            if not isinstance(evidence, dict) or len(_json_bytes(evidence)) > MAX_BODY_BYTES:
                raise ControlApiError("CHECKPOINT_EVIDENCE_INVALID")
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                deployment = connection.execute(
                    "SELECT phase, sequence, state "
                    "FROM agent_control_deployments WHERE deployment_id = %s",
                    (deployment_id,),
                ).fetchone()
            if not deployment:
                raise ControlApiError("DEPLOYMENT_NOT_FOUND")
            stored_state = deployment["state"] if isinstance(deployment["state"], dict) else {}
            stored_evidence = stored_state.get("evidence")
            plan_evidence = evidence
            if not plan_evidence and isinstance(stored_evidence, dict):
                plan_evidence = stored_evidence
            stored_artifacts = stored_state.get("artifacts")
            if isinstance(stored_artifacts, dict):
                plan_evidence = {**plan_evidence, **stored_artifacts}
            plan = plan_deployment_command(
                deployment_id,
                deployment["phase"],
                spec,
                plan_evidence,
            )
            self._send(200, {
                "deployment_id": str(deployment_id),
                "phase": deployment["phase"],
                "sequence": deployment["sequence"],
                "terminal": plan["terminal"],
                "next_step": plan["next_step"],
            })

        def _reconcile_deployments(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            limit = document.get("limit", 100)
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                summary = reconcile_deployments(connection, limit)
            if summary["status"] == "invalid_limit":
                raise ControlApiError("RECONCILE_LIMIT_INVALID")
            self._send(200, summary)

        def _enqueue(self, document: dict[str, Any]) -> None:
            self._require_platform_api()
            deployment_id = _parse_uuid(document.get("deployment_id"), "DEPLOYMENT_ID")
            target_agent_id = _parse_uuid(document.get("target_agent_id"), "TARGET_AGENT_ID")
            role = document.get("required_role")
            action = document.get("action")
            idempotency_key = document.get("idempotency_key")
            payload = document.get("payload", {})
            follow_plan = document.get("follow_plan", False)
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
            if not isinstance(follow_plan, bool):
                raise ControlApiError("COMMAND_FOLLOW_PLAN_INVALID")
            command_id = uuid4()
            command_status = "queued"
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                agent = connection.execute(
                    "SELECT role, enabled, cluster_id, installation_id "
                    "FROM agent_control_agents WHERE agent_id = %s",
                    (target_agent_id,),
                ).fetchone()
                deployment = connection.execute(
                    "SELECT deployment_id, installation_id, cluster_id, phase "
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
                from_phase = deployment["phase"]
                next_phase = None
                if follow_plan:
                    plan = plan_deployment_command(
                        deployment_id,
                        deployment["phase"],
                        payload,
                    )
                    next_step = plan["next_step"]
                    if next_step is None:
                        raise ControlApiError("DEPLOYMENT_ALREADY_TERMINAL")
                    if (
                        next_step["role"] != role
                        or next_step["action"] != action
                        or next_step["idempotency_key"] != idempotency_key
                        or next_step["payload"] != payload
                    ):
                        raise ControlApiError("COMMAND_DOES_NOT_MATCH_PLAN")
                    next_phase = next_step["next_phase"]
                else:
                    inferred = next_phase_for_action(from_phase, action)
                    next_phase = inferred.value if inferred is not None else None
                inserted = connection.execute(
                    """
                    INSERT INTO agent_control_commands (
                        command_id, deployment_id, target_agent_id, required_role,
                        action, idempotency_key, payload, from_phase, next_phase, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING command_id
                    """,
                    (
                        command_id, deployment_id, target_agent_id, role, action,
                        idempotency_key, Jsonb(payload), from_phase, next_phase,
),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        """
                        SELECT command_id, deployment_id, target_agent_id, required_role,
                               action, payload, status
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
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
            ):
                raise ControlApiError("HEARTBEAT_SEQUENCE_INVALID")
            if readiness not in ("ready", "degraded", "not_ready"):
                raise ControlApiError("HEARTBEAT_READINESS_INVALID")
            capabilities = _validate_reported_capabilities(
                identity["role"],
                capabilities,
            )
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
                               c.payload, c.next_phase AS command_next_phase,
                               c.created_at, c.deadline,
                               d.phase AS deployment_phase,
                               d.sequence AS checkpoint_sequence,
                               d.state AS checkpoint_state,
                               d.trusted_context AS trusted_context
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
            command_payload = _command_payload_for_claim(
                row["payload"],
                row["command_next_phase"],
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
                    "payload": command_payload,
                    "deployment_checkpoint": {
                        "phase": row["deployment_phase"],
                        "sequence": row["checkpoint_sequence"],
                        "state": row["checkpoint_state"],
                    },
                    "deployment_context": (
                        row["trusted_context"] or {}
                        if identity["role"] == "target"
                        else {}
                    ),
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
            _validate_checkpoint_evidence(identity["role"], next_phase, evidence)
            _validate_checkpoint_actor_transition(
                identity["role"],
                expected_phase,
                next_phase,
            )
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
                        SELECT deployment_id, required_role, from_phase, next_phase
                        FROM agent_control_commands
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
                    _validate_checkpoint_command_transition(
                        leased_command,
                        expected_phase,
                        next_phase,
                        identity["role"],
                    )
                    state = {
                        "state_digest": state_digest.lower(),
                        "evidence": evidence,
                    }
                    artifacts = _merge_planning_artifacts(
                        deployment["state"],
                        evidence,
                    )
                    if artifacts:
                        state["artifacts"] = artifacts
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
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                with connection.transaction():
                    updated = connection.execute(
                        """
                        UPDATE agent_control_commands
                        SET status = %s, result = %s, updated_at = NOW()
                        WHERE command_id = %s AND target_agent_id = %s
                          AND lease_id = %s AND lease_token_hash = %s
                          AND status = 'leased' AND lease_expires_at > NOW()
                        RETURNING deployment_id, action, required_role
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
                            lease_id,
                            token_hash,
                        ),
                    ).fetchone()
                    if updated is None:
                        raise ControlApiError("COMMAND_LEASE_NOT_VALID")
                    if (
                        status == "succeeded"
                        and identity["role"] == "target"
                        and updated["required_role"] == "target"
                        and updated["action"] == "target.capi.pivot"
                    ):
                        attestation = (
                            _validated_eph_pivot_attestation(document.get("output"))
                            if (
                                document.get("retryable") is False
                                and (
                                    document.get("side_effects") is True
                                    or document.get("output", {}).get("already_pivoted") is True
                                )
                            )
                            else None
                        )
                        if attestation is None:
                            invalid_result = {
                                key: value
                                for key, value in document.items()
                                if key != "lease_token"
                            }
                            invalid_result["status"] = "blocked"
                            invalid_result["error"] = "EPH_PIVOT_ATTESTATION_INVALID"
                            connection.execute(
                                """
                                UPDATE agent_control_commands
                                SET status = 'blocked', result = %s, updated_at = NOW()
                                WHERE command_id = %s AND target_agent_id = %s
                                  AND lease_id = %s AND lease_token_hash = %s
                                """,
                                (
                                    Jsonb(invalid_result),
                                    command_id,
                                    identity["agent_id"],
                                    lease_id,
                                    token_hash,
                                ),
                            )
                        else:
                            deployment = connection.execute(
                                """
                                SELECT trusted_context
                                FROM agent_control_deployments
                                WHERE deployment_id = %s
                                FOR UPDATE
                                """,
                                (updated["deployment_id"],),
                            ).fetchone()
                            if deployment is None:
                                raise ControlApiError("PIVOT_DEPLOYMENT_NOT_FOUND")
                            trusted_context = deployment["trusted_context"] or {}
                            trusted_context = {
                                **trusted_context,
                                "eph_pivot_attestation": {
                                    **attestation,
                                    "source_agent_id": str(identity["agent_id"]),
                                    "command_id": str(command_id),
                                    "attested_at": datetime.now(UTC).isoformat(),
                                },
                            }
                            connection.execute(
                                """
                                UPDATE agent_control_deployments
                                SET trusted_context = %s, updated_at = NOW()
                                WHERE deployment_id = %s
                                """,
                                (Jsonb(trusted_context), updated["deployment_id"]),
                            )
            try:
                with psycopg.connect(database_url, row_factory=dict_row) as connection:
                    advance_after_command(connection, updated["deployment_id"], command_id)
            except psycopg.Error:
                # The result is already durable. Auto-advance can be retried by
                # the dedicated recovery path without returning a false failure.
                pass
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
