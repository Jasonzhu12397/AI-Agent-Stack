"""Read the Rust-owned platform state without importing the retired Python API.

The Metal3 command-line tools run on EPH, Target and operator hosts where the
Rust API may not be reachable, so they read the shared database directly. The
module keeps SQLite for Codespaces and PostgreSQL for production behind one
small interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

GLOBAL_AUDIT_CHAIN_LOCK_ID = 930417
SENSITIVE_METADATA_PARTS = (
    "password",
    "secret",
    "token",
    "private_key",
    "kubeconfig",
    "api_key",
    "access_key",
    "authorization",
    "credential",
)


def database_url(explicit: str | None = None) -> str:
    value = (explicit or os.getenv("DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required to read platform state.")
    return value


def _sqlite_path(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if path.startswith("//"):
        path = path[1:]
    if not path:
        raise ValueError("SQLite DATABASE_URL has no path.")
    return path


@contextmanager
def connect(explicit: str | None = None) -> Iterator[tuple[Any, bool]]:
    """Yield a DB-API connection and whether the dialect is SQLite."""

    url = database_url(explicit)
    if url.startswith("sqlite:"):
        path = _sqlite_path(url)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection, True
        finally:
            connection.close()
        return
    if url.startswith(("postgres:", "postgresql:")):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - production image installs it
            raise RuntimeError("psycopg is required for PostgreSQL DATABASE_URL.") from exc
        connection = psycopg.connect(url)
        try:
            yield connection, False
        finally:
            connection.close()
        return
    raise ValueError("DATABASE_URL must be sqlite:// or postgresql://.")


def _rows(cursor: Any, sqlite: bool) -> list[dict[str, Any]]:
    if sqlite:
        return [dict(row) for row in cursor.fetchall()]
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def platform_machines(
    project_id: str | None = None,
    explicit: str | None = None,
) -> list[dict[str, Any]]:
    """Return every AgentMachine joined with its agent node."""

    with connect(explicit) as (connection, sqlite):
        placeholder = "?" if sqlite else "%s"
        sql = (
            "SELECT CAST(m.id AS TEXT) AS id, CAST(m.tenant_id AS TEXT) AS tenant_id, "
            "CAST(m.project_id AS TEXT) AS project_id, m.bmh_name, m.lifecycle_state, "
            "m.identity_status, m.bmc_endpoint, m.bmc_credential_ref, "
            "m.bmc_trust_bundle_ref, m.bmc_tls_verify, m.hardware_inventory, "
            "CAST(m.agent_node_id AS TEXT) AS agent_node_id, n.name AS agent_node_name, "
            "n.labels AS agent_node_labels "
            "FROM core_agentmachine m LEFT JOIN core_agentnode n ON n.id = m.agent_node_id"
        )
        parameters: tuple[Any, ...] = ()
        if project_id:
            sql += f" WHERE CAST(m.project_id AS TEXT) = {placeholder}"
            parameters = (project_id,)
        sql += " ORDER BY n.name"
        cursor = connection.execute(sql, parameters)
        rows = _rows(cursor, sqlite)
    for row in rows:
        row["hardware_inventory"] = _json(row.get("hardware_inventory"), {})
        row["agent_node_labels"] = _json(row.get("agent_node_labels"), {})
        row["bmc_tls_verify"] = bool(row.get("bmc_tls_verify"))
    return rows


def cluster_deployment(deployment_id: str, explicit: str | None = None) -> dict[str, Any] | None:
    with connect(explicit) as (connection, sqlite):
        placeholder = "?" if sqlite else "%s"
        cursor = connection.execute(
            "SELECT CAST(id AS TEXT) AS id, status, error, plan_result "
            f"FROM core_clusterdeployment WHERE CAST(id AS TEXT) = {placeholder}",
            (deployment_id,),
        )
        rows = _rows(cursor, sqlite)
    if not rows:
        return None
    row = rows[0]
    row["plan_result"] = _json(row.get("plan_result"), {})
    return row


def update_machine(
    machine_id: str,
    *,
    bmh_name: str | None = None,
    lifecycle_state: str | None = None,
    explicit: str | None = None,
) -> bool:
    """Update the two planning columns the fleet writer owns."""

    assignments: list[str] = []
    parameters: list[Any] = []
    with connect(explicit) as (connection, sqlite):
        placeholder = "?" if sqlite else "%s"
        if bmh_name is not None:
            assignments.append(f"bmh_name = {placeholder}")
            parameters.append(bmh_name)
        if lifecycle_state is not None:
            assignments.append(f"lifecycle_state = {placeholder}")
            parameters.append(lifecycle_state)
        if not assignments:
            return False
        now = "CURRENT_TIMESTAMP"
        assignments.append(f"updated_at = {now}")
        parameters.append(machine_id)
        cursor = connection.execute(
            f"UPDATE core_agentmachine SET {', '.join(assignments)} "
            f"WHERE CAST(id AS TEXT) = {placeholder}",
            tuple(parameters),
        )
        connection.commit()
        return cursor.rowcount > 0


def machine_context(machine_id: str, explicit: str | None = None) -> dict[str, str] | None:
    """Return the tenant and project that own a machine."""

    with connect(explicit) as (connection, sqlite):
        placeholder = "?" if sqlite else "%s"
        cursor = connection.execute(
            "SELECT CAST(tenant_id AS TEXT) AS tenant_id, "
            "CAST(project_id AS TEXT) AS project_id "
            f"FROM core_agentmachine WHERE CAST(id AS TEXT) = {placeholder}",
            (machine_id,),
        )
        rows = _rows(cursor, sqlite)
    return rows[0] if rows else None


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, child in value.items():
        normalized = str(key).lower().replace("-", "_")
        if any(part in normalized for part in SENSITIVE_METADATA_PARTS):
            continue
        name = str(key)[:80]
        if isinstance(child, dict):
            result[name] = _safe_metadata(child)
        elif isinstance(child, list):
            result[name] = [
                item
                for item in child[:50]
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
        elif isinstance(child, (str, int, float, bool)) or child is None:
            result[name] = child
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("Audit metadata exceeds 8 KiB.")
    return result


def record_audit_event(
    *,
    tenant_id: str | None,
    project_id: str | None,
    event_type: str,
    resource_type: str,
    resource_id: str = "",
    metadata: dict[str, Any] | None = None,
    explicit: str | None = None,
) -> str:
    """Append one event to the tenant audit hash chain.

    The canonical document must match the Rust implementation byte for byte,
    otherwise chain verification rejects the appended event.
    """

    safe_metadata = _safe_metadata(metadata or {})
    timestamp = datetime.now(UTC)
    created_at = timestamp.isoformat()
    event_id = uuid.uuid4()
    with connect(explicit) as (connection, sqlite):
        if sqlite:
            connection.execute("BEGIN IMMEDIATE")
        else:
            if tenant_id:
                connection.execute(
                    "SELECT id FROM core_tenant WHERE id = %s::uuid FOR UPDATE",
                    (tenant_id,),
                )
            else:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (GLOBAL_AUDIT_CHAIN_LOCK_ID,),
                )
        previous = connection.execute(
            "SELECT event_hash FROM core_auditevent WHERE "
            + ("tenant_id = ?" if sqlite else "tenant_id = %s::uuid")
            + " ORDER BY created_at DESC, id DESC LIMIT 1"
            if tenant_id
            else "SELECT event_hash FROM core_auditevent WHERE tenant_id IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (tenant_id,) if tenant_id else (),
        ).fetchone()
        previous_hash = (previous[0] if previous else "") or ""
        canonical = {
            "id": str(event_id),
            "tenant_id": tenant_id,
            "project_id": project_id,
            "actor_id": None,
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": str(resource_id)[:64],
            "outcome": "success",
            "request_id": "",
            "remote_addr": None,
            "user_agent": "",
            "metadata": safe_metadata,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        values = (
            str(event_id),
            tenant_id,
            project_id,
            None,
            event_type,
            resource_type,
            str(resource_id)[:64],
            "success",
            "",
            None,
            "",
            json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":")),
            previous_hash,
            event_hash,
            created_at,
        )
        if sqlite:
            connection.execute(
                "INSERT INTO core_auditevent ("
                "id, tenant_id, project_id, actor_id, event_type, resource_type, "
                "resource_id, outcome, request_id, remote_addr, user_agent, metadata, "
                "previous_hash, event_hash, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            sql = (
                "INSERT INTO core_auditevent ("
                "id, tenant_id, project_id, actor_id, event_type, resource_type, "
                "resource_id, outcome, request_id, remote_addr, user_agent, metadata, "
                "previous_hash, event_hash, created_at"
                ") VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s::jsonb, %s, %s, %s::timestamptz)"
            )
            connection.execute(sql, values)
        connection.commit()
    return event_hash
