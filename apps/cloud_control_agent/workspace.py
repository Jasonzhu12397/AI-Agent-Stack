"""Project workspace contracts for the Cloud Control Agent.

A project is an installation-scoped container for reusable skills, plugins and
units of work. The control plane stores manifests and references only; it never
stores plugin credentials or skill payload secrets. Agents claim work over their
mTLS identity and receive the enabled skill/plugin catalog for the project.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_NAME_LENGTH = 63
MAX_VERSION_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 512
MAX_MANIFEST_BYTES = 64 * 1024
MAX_WORK_PAYLOAD_BYTES = 256 * 1024
MAX_ACTIONS = 32
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_WORKFLOW_STEPS = 32
MAX_STEP_DEPENDENCIES = 8
MAX_STEP_NAME_LENGTH = 128
MAX_WORKFLOW_DESCRIPTION_LENGTH = 512
DEFAULT_WORKFLOW_DEADLINE_SECONDS = 86400
MIN_WORKFLOW_DEADLINE_SECONDS = 60
MAX_WORKFLOW_DEADLINE_SECONDS = 604800
MAX_APPROVAL_ACTION_LENGTH = 128
MAX_APPROVER_LENGTH = 128
MAX_DECISION_REASON_LENGTH = 512
APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "used", "expired"})
DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
CONFIG_REF_PATTERN = re.compile(r"^(?:secret|vault|config)://[A-Za-z0-9._/-]{1,120}$")
WORK_KINDS = frozenset({"deployment", "maintenance", "inspection", "diagnostic", "software"})
PLUGIN_KINDS = frozenset({"connector", "tool", "runtime"})
PROJECT_STATUSES = frozenset({"active", "archived"})
TASK_STATUSES = frozenset(
    {"blocked", "queued", "leased", "awaiting_approval", "succeeded", "failed", "cancelled"}
)
WORKFLOW_STEP_TYPES = frozenset({"task", "approval", "loop", "condition"})
CONDITION_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*$")
MAX_LOOP_ITERATIONS = 10
WORKFLOW_JOIN_POLICIES = frozenset({"all", "any"})
WORKFLOW_RUN_STATUSES = frozenset({"running", "succeeded", "failed", "cancelled"})
AGENT_ROLES = frozenset({"eph", "target"})


class WorkspaceError(ValueError):
    """A workspace document violates the control-plane contract."""


def _bounded_text(value: Any, field: str, *, maximum: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise WorkspaceError(f"{field} must be a string.")
    text = value.strip()
    if required and not text:
        raise WorkspaceError(f"{field} is required.")
    if len(text) > maximum:
        raise WorkspaceError(f"{field} exceeds {maximum} characters.")
    return text


def _bounded_json(value: Any, field: str, *, maximum: int) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkspaceError(f"{field} must be a JSON object.")
    try:
        encoded = json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(f"{field} is not JSON serialisable.") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise WorkspaceError(f"{field} exceeds {maximum} bytes.")
    return value


def normalize_name(value: Any, field: str = "name") -> str:
    text = _bounded_text(value, field, maximum=MAX_NAME_LENGTH)
    if not NAME_PATTERN.fullmatch(text):
        raise WorkspaceError(
            f"{field} must start with a letter or digit and may contain "
            "only letters, digits, '.', '_' or '-'."
        )
    return text


def normalize_version(value: Any) -> str:
    text = _bounded_text(value, "version", maximum=MAX_VERSION_LENGTH)
    if not VERSION_PATTERN.fullmatch(text):
        raise WorkspaceError("version contains unsupported characters.")
    return text


def normalize_project(
    *,
    name: Any,
    description: Any = "",
    metadata: Any = None,
    status: Any = "active",
) -> dict[str, Any]:
    if status not in PROJECT_STATUSES:
        raise WorkspaceError("project status must be active or archived.")
    return {
        "name": normalize_name(name),
        "description": _bounded_text(
            description,
            "description",
            maximum=MAX_DESCRIPTION_LENGTH,
            required=False,
        ),
        "metadata": _bounded_json(metadata, "metadata", maximum=MAX_MANIFEST_BYTES),
        "status": status,
    }


def normalize_skill(
    *,
    name: Any,
    version: Any,
    description: Any = "",
    manifest: Any,
) -> dict[str, Any]:
    """Validate a skill manifest without interpreting its implementation."""

    document = _bounded_json(manifest, "manifest", maximum=MAX_MANIFEST_BYTES)
    entrypoint = document.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise WorkspaceError("manifest.entrypoint is required.")
    if len(entrypoint) > 160:
        raise WorkspaceError("manifest.entrypoint exceeds 160 characters.")
    actions = document.get("actions", [])
    if (
        not isinstance(actions, list)
        or len(actions) > MAX_ACTIONS
        or any(not isinstance(item, str) or not ACTION_PATTERN.fullmatch(item) for item in actions)
        or len(actions) != len(set(actions))
    ):
        raise WorkspaceError("manifest.actions must be unique dotted action names.")
    for key in ("inputs", "outputs"):
        if key in document:
            _bounded_json(document[key], f"manifest.{key}", maximum=MAX_MANIFEST_BYTES)
    return {
        "name": normalize_name(name),
        "version": normalize_version(version),
        "description": _bounded_text(
            description,
            "description",
            maximum=MAX_DESCRIPTION_LENGTH,
            required=False,
        ),
        "manifest": document,
    }


def normalize_plugin(
    *,
    name: Any,
    version: Any,
    kind: Any,
    description: Any = "",
    config_ref: Any = None,
    manifest: Any = None,
) -> dict[str, Any]:
    """Validate a plugin registration; credentials stay behind ``config_ref``."""

    if kind not in PLUGIN_KINDS:
        raise WorkspaceError("plugin kind must be connector, tool or runtime.")
    reference = ""
    if config_ref not in (None, ""):
        reference = _bounded_text(config_ref, "config_ref", maximum=160)
        if not CONFIG_REF_PATTERN.fullmatch(reference):
            raise WorkspaceError(
                "config_ref must be secret://, vault:// or config:// "
                "and must not inline credentials."
            )
    return {
        "name": normalize_name(name),
        "version": normalize_version(version),
        "kind": kind,
        "description": _bounded_text(
            description,
            "description",
            maximum=MAX_DESCRIPTION_LENGTH,
            required=False,
        ),
        "config_ref": reference,
        "manifest": _bounded_json(manifest, "manifest", maximum=MAX_MANIFEST_BYTES),
    }


def normalize_work(
    *,
    kind: Any,
    payload: Any,
    required_role: Any = None,
    priority: Any = 0,
    idempotency_key: Any,
) -> dict[str, Any]:
    if kind not in WORK_KINDS:
        raise WorkspaceError(
            "work kind must be deployment, maintenance, inspection, diagnostic or software."
        )
    if required_role not in (None, "", *AGENT_ROLES):
        raise WorkspaceError("required_role must be eph, target or empty.")
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0 or priority > 9:
        raise WorkspaceError("priority must be an integer from 0 to 9.")
    key = _bounded_text(
        idempotency_key,
        "idempotency_key",
        maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
    )
    return {
        "kind": kind,
        "payload": _bounded_json(payload, "payload", maximum=MAX_WORK_PAYLOAD_BYTES),
        "required_role": required_role or None,
        "priority": priority,
        "idempotency_key": key,
    }


def normalize_approval(
    *,
    work_id: Any,
    action: Any,
    payload_digest: Any,
    requested_by: Any,
    ttl_seconds: Any = 3600,
) -> dict[str, Any]:
    work_uuid = _bounded_text(work_id, "work_id", maximum=64)
    action_text = _bounded_text(action, "action", maximum=MAX_APPROVAL_ACTION_LENGTH)
    digest = _bounded_text(payload_digest, "payload_digest", maximum=64)
    if not DIGEST_PATTERN.fullmatch(digest):
        raise WorkspaceError("payload_digest must be a lowercase sha256 hex digest.")
    requester = _bounded_text(requested_by, "requested_by", maximum=MAX_APPROVER_LENGTH)
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or not 60 <= ttl_seconds <= 86400
    ):
        raise WorkspaceError("ttl_seconds must be an integer from 60 to 86400.")
    return {
        "work_id": work_uuid,
        "action": action_text,
        "payload_digest": digest,
        "requested_by": requester,
        "ttl_seconds": ttl_seconds,
    }


def normalize_work_result(*, status: Any, result: Any) -> dict[str, Any]:
    if status not in ("succeeded", "failed"):
        raise WorkspaceError("work result status must be succeeded or failed.")
    return {
        "status": status,
        "result": _bounded_json(result, "result", maximum=MAX_WORK_PAYLOAD_BYTES),
    }


def normalize_workflow_definition(definition: Any) -> dict[str, Any]:
    """Validate a project workflow as an acyclic dependency graph of tasks."""

    document = _bounded_json(definition, "definition", maximum=MAX_MANIFEST_BYTES)
    steps = document.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
        raise WorkspaceError(f"definition.steps must contain 1 to {MAX_WORKFLOW_STEPS} steps.")
    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise WorkspaceError(f"definition.steps[{index}] must be an object.")
        step_id = normalize_name(item.get("id"), f"definition.steps[{index}].id")
        if step_id in seen:
            raise WorkspaceError(f"duplicate workflow step id: {step_id}")
        seen.add(step_id)
        name = _bounded_text(
            item.get("name"),
            f"definition.steps[{index}].name",
            maximum=MAX_STEP_NAME_LENGTH,
        )
        role = item.get("required_role")
        if role not in (None, "", *AGENT_ROLES):
            raise WorkspaceError(f"definition.steps[{index}].required_role is invalid.")
        join_policy = item.get("join", "all")
        if join_policy not in WORKFLOW_JOIN_POLICIES:
            raise WorkspaceError(
                f"definition.steps[{index}].join must be all or any."
            )
        dependencies = item.get("depends_on", [])
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > MAX_STEP_DEPENDENCIES
            or any(not isinstance(dep, str) or not dep for dep in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise WorkspaceError(f"definition.steps[{index}].depends_on must be unique step ids.")
        when = item.get("when", {})
        if not isinstance(when, dict) or len(when) > MAX_STEP_DEPENDENCIES:
            raise WorkspaceError(
                f"definition.steps[{index}].when must be an object of at most "
                f"{MAX_STEP_DEPENDENCIES} conditions."
            )
        for dependency_id, expected in when.items():
            if dependency_id not in dependencies:
                raise WorkspaceError(
                    f"definition.steps[{index}].when references {dependency_id}, "
                    "which is not a dependency."
                )
            if not isinstance(expected, bool):
                raise WorkspaceError(
                    f"definition.steps[{index}].when.{dependency_id} must be a boolean."
                )
        step_type = item.get("type", "task")
        condition = item.get("condition")
        if step_type == "condition":
            if not isinstance(condition, dict):
                raise WorkspaceError(
                    f"definition.steps[{index}].condition must be an object."
                )
            if "equals" not in condition:
                raise WorkspaceError(
                    f"definition.steps[{index}].condition.equals is required."
                )
            expected_value = condition["equals"]
            if isinstance(expected_value, (dict, list)):
                raise WorkspaceError(
                    f"definition.steps[{index}].condition.equals must be a scalar."
                )
            field_name = _bounded_text(
                condition.get("field"),
                f"definition.steps[{index}].condition.field",
                maximum=128,
            )
            if not CONDITION_FIELD_PATTERN.fullmatch(field_name):
                raise WorkspaceError(
                    f"definition.steps[{index}].condition.field must be a dotted path."
                )
            source = condition.get("source")
            if source is not None:
                source = normalize_name(
                    source, f"definition.steps[{index}].condition.source"
                )
                if source not in dependencies:
                    raise WorkspaceError(
                        f"definition.steps[{index}].condition.source must be a dependency."
                    )
            condition = {"source": source, "field": field_name, "equals": expected_value}
        else:
            condition = None
        if step_id in dependencies:
            raise WorkspaceError(f"workflow step {step_id} cannot depend on itself.")
        if step_type not in WORKFLOW_STEP_TYPES:
            raise WorkspaceError(
                f"definition.steps[{index}].type must be task, approval, loop or "
                "condition."
            )
        if step_type in {"approval", "condition"} and role not in (None, ""):
            raise WorkspaceError(
                f"definition.steps[{index}].required_role must be empty for "
                f"{step_type} steps."
            )
        max_iterations = item.get("max_iterations", 1)
        if (
            not isinstance(max_iterations, int)
            or isinstance(max_iterations, bool)
            or not 1 <= max_iterations <= MAX_LOOP_ITERATIONS
        ):
            raise WorkspaceError(
                f"definition.steps[{index}].max_iterations must be an integer "
                f"from 1 to {MAX_LOOP_ITERATIONS}."
            )
        if step_type == "loop" and max_iterations < 2:
            raise WorkspaceError(
                f"definition.steps[{index}].max_iterations must be at least 2 "
                "for loop steps."
            )
        if step_type == "condition" and max_iterations != 1:
            raise WorkspaceError(
                f"definition.steps[{index}].max_iterations must be 1 for condition steps."
            )
        if step_type not in {"loop", "condition"} and max_iterations != 1:
            raise WorkspaceError(
                f"definition.steps[{index}].max_iterations is only valid for "
                "loop steps."
            )
        max_attempts = item.get("max_attempts", 1)
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 5
        ):
            raise WorkspaceError(
                f"definition.steps[{index}].max_attempts must be an integer from 1 to 5."
            )
        if step_type == "approval" and max_attempts != 1:
            raise WorkspaceError(
                f"definition.steps[{index}].max_attempts must be 1 for approval steps."
            )
        normalized_steps.append(
            {
                "id": step_id,
                "name": name,
                "type": step_type,
                "join": join_policy,
                "when": dict(when),
                "condition": condition,
                "max_iterations": max_iterations,
                "required_role": role or None,
                "payload": _bounded_json(
                    item.get("payload"),
                    f"definition.steps[{index}].payload",
                    maximum=MAX_WORK_PAYLOAD_BYTES,
                ),
                "depends_on": list(dependencies),
                "max_attempts": max_attempts,
            }
        )
    step_ids = {step["id"] for step in normalized_steps}
    for step in normalized_steps:
        missing = [dep for dep in step["depends_on"] if dep not in step_ids]
        if missing:
            raise WorkspaceError(
                f"workflow step {step['id']} references unknown step {missing[0]}."
            )
        types = {item["id"]: item["type"] for item in normalized_steps}
        for dependency_id in step["when"]:
            if types.get(dependency_id) != "condition":
                raise WorkspaceError(
                    f"workflow step {step['id']}.when requires a condition dependency, "
                    f"but {dependency_id} is not a condition step."
                )
    _assert_acyclic(normalized_steps)
    description = _bounded_text(
        document.get("description", ""),
        "definition.description",
        maximum=MAX_WORKFLOW_DESCRIPTION_LENGTH,
        required=False,
    )
    return {"description": description, "steps": normalized_steps}


def _assert_acyclic(steps: list[dict[str, Any]]) -> None:
    by_id = {step["id"]: step for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise WorkspaceError(f"workflow contains a cycle at step {step_id}.")
        visiting.add(step_id)
        for dependency in by_id[step_id]["depends_on"]:
            visit(dependency)
        visiting.discard(step_id)
        visited.add(step_id)

    for step in steps:
        visit(step["id"])


def normalize_task(
    *,
    name: Any,
    payload: Any,
    required_role: Any = None,
    priority: Any = 0,
    idempotency_key: Any,
) -> dict[str, Any]:
    if required_role not in (None, "", *AGENT_ROLES):
        raise WorkspaceError("required_role must be eph, target or empty.")
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0 or priority > 9:
        raise WorkspaceError("priority must be an integer from 0 to 9.")
    return {
        "name": _bounded_text(name, "name", maximum=MAX_STEP_NAME_LENGTH),
        "payload": _bounded_json(payload, "payload", maximum=MAX_WORK_PAYLOAD_BYTES),
        "required_role": required_role or None,
        "priority": priority,
        "idempotency_key": _bounded_text(
            idempotency_key,
            "idempotency_key",
            maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
    }


def normalize_workflow_run(
    *, input: Any, idempotency_key: Any, deadline_seconds: Any = None
) -> dict[str, Any]:
    if deadline_seconds is None:
        deadline_seconds = DEFAULT_WORKFLOW_DEADLINE_SECONDS
    if (
        not isinstance(deadline_seconds, int)
        or isinstance(deadline_seconds, bool)
        or not MIN_WORKFLOW_DEADLINE_SECONDS
        <= deadline_seconds
        <= MAX_WORKFLOW_DEADLINE_SECONDS
    ):
        raise WorkspaceError(
            f"deadline_seconds must be an integer from "
            f"{MIN_WORKFLOW_DEADLINE_SECONDS} to {MAX_WORKFLOW_DEADLINE_SECONDS}."
        )
    return {
        "input": _bounded_json(input, "input", maximum=MAX_WORK_PAYLOAD_BYTES),
        "idempotency_key": _bounded_text(
            idempotency_key,
            "idempotency_key",
            maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
        "deadline_seconds": deadline_seconds,
    }


def normalize_workflow_decision(*, decided_by: Any, reason: Any = "") -> dict[str, Any]:
    return {
        "decided_by": _bounded_text(
            decided_by,
            "decided_by",
            maximum=MAX_APPROVER_LENGTH,
        ),
        "reason": _bounded_text(
            reason,
            "reason",
            maximum=MAX_DECISION_REASON_LENGTH,
            required=False,
        ),
    }


def normalize_workspace_path(raw: str) -> tuple[str, str | None]:
    """Split ``/api/v1/control/projects/<id>[/skills|plugins|work|tasks|workflows]``."""

    prefix = "/api/v1/control/projects/"
    if not raw.startswith(prefix):
        raise WorkspaceError("not a project path")
    remainder = raw[len(prefix) :].strip("/")
    if not remainder:
        raise WorkspaceError("project id is required")
    parts = remainder.split("/")
    if len(parts) > 2:
        raise WorkspaceError("project path is invalid")
    project_id = parts[0]
    collection = parts[1] if len(parts) == 2 else None
    if collection not in (
        None,
        "skills",
        "plugins",
        "work",
        "tasks",
        "workflows",
        "approvals",
    ):
        raise WorkspaceError("project collection is invalid")
    return project_id, collection
