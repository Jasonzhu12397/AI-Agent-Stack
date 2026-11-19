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

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
CONFIG_REF_PATTERN = re.compile(r"^(?:secret|vault|config)://[A-Za-z0-9._/-]{1,120}$")
WORK_KINDS = frozenset({"deployment", "maintenance", "inspection", "diagnostic"})
PLUGIN_KINDS = frozenset({"connector", "tool", "runtime"})
PROJECT_STATUSES = frozenset({"active", "archived"})
WORK_STATUSES = frozenset({"queued", "leased", "succeeded", "failed", "cancelled"})
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
            "work kind must be deployment, maintenance, inspection or diagnostic."
        )
    if required_role not in (None, "", *AGENT_ROLES):
        raise WorkspaceError("required_role must be eph, target or empty.")
    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or priority < 0
        or priority > 9
    ):
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


def normalize_work_result(*, status: Any, result: Any) -> dict[str, Any]:
    if status not in ("succeeded", "failed"):
        raise WorkspaceError("work result status must be succeeded or failed.")
    return {
        "status": status,
        "result": _bounded_json(result, "result", maximum=MAX_WORK_PAYLOAD_BYTES),
    }


def normalize_workspace_path(raw: str) -> tuple[str, str | None]:
    """Split ``/api/v1/control/projects/<id>[/skills|plugins|work]``."""

    prefix = "/api/v1/control/projects/"
    if not raw.startswith(prefix):
        raise WorkspaceError("not a project path")
    remainder = raw[len(prefix):].strip("/")
    if not remainder:
        raise WorkspaceError("project id is required")
    parts = remainder.split("/")
    if len(parts) > 2:
        raise WorkspaceError("project path is invalid")
    project_id = parts[0]
    collection = parts[1] if len(parts) == 2 else None
    if collection not in (None, "skills", "plugins", "work"):
        raise WorkspaceError("project collection is invalid")
    return project_id, collection
