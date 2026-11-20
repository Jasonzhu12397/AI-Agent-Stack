from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

from .api import serve
from .deployment_controller import reconcile_deployments


class CloudControlConfigurationError(ValueError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CloudControlConfigurationError(f"{name} is required")
    return value


def _database_url() -> str:
    value = _required("DATABASE_URL")
    if not value.startswith(("postgresql://", "postgres://")):
        raise CloudControlConfigurationError("DATABASE_URL must use PostgreSQL")
    return value


def initialize_store(connection: psycopg.Connection[Any]) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_deployments (
            deployment_id UUID PRIMARY KEY,
            installation_id UUID NOT NULL,
            cluster_id UUID NOT NULL,
            phase TEXT NOT NULL,
            sequence BIGINT NOT NULL DEFAULT 0,
            state JSONB NOT NULL DEFAULT '{}'::jsonb,
            spec JSONB NOT NULL DEFAULT '{}'::jsonb,
            trusted_context JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute(
        "ALTER TABLE agent_control_deployments "
        "ADD COLUMN IF NOT EXISTS trusted_context JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    connection.execute(
        "ALTER TABLE agent_control_deployments "
        "ADD COLUMN IF NOT EXISTS spec JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_agents (
            agent_id UUID PRIMARY KEY,
            installation_id UUID NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('eph', 'target')),
            cluster_id UUID,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            instance_id TEXT,
            heartbeat_sequence BIGINT NOT NULL DEFAULT 0,
            readiness TEXT NOT NULL DEFAULT 'not_ready',
            capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_seen TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK ((role = 'target' AND cluster_id IS NOT NULL) OR
                   (role = 'eph' AND cluster_id IS NULL))
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_commands (
            command_id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES agent_control_deployments(deployment_id),
            target_agent_id UUID NOT NULL REFERENCES agent_control_agents(agent_id),
            required_role TEXT NOT NULL CHECK (required_role IN ('eph', 'target')),
            action TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload JSONB NOT NULL,
            from_phase TEXT,
            next_phase TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'leased', 'succeeded', 'failed', 'blocked', 'cancelled')
            ),
            lease_id UUID,
            lease_token_hash TEXT,
            lease_expires_at TIMESTAMPTZ,
            issued_at TIMESTAMPTZ,
            deadline TIMESTAMPTZ,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS lease_token_hash TEXT"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS deadline TIMESTAMPTZ"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS from_phase TEXT"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands ADD COLUMN IF NOT EXISTS next_phase TEXT"
    )
    connection.execute(
        "ALTER TABLE agent_control_commands "
        "ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_commands_claim_idx
        ON agent_control_commands (required_role, target_agent_id, status, created_at)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_agents_last_seen_idx
        ON agent_control_agents (last_seen)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_projects (
            project_id UUID PRIMARY KEY,
            installation_id UUID NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (installation_id, name)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_skills (
            skill_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            manifest JSONB NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (project_id, name, version)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_plugins (
            plugin_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('connector', 'tool', 'runtime')),
            description TEXT NOT NULL DEFAULT '',
            config_ref TEXT NOT NULL DEFAULT '',
            manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (project_id, name, version)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_work_items (
            work_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'deployment', 'maintenance', 'inspection', 'diagnostic', 'software'
                )
            ),
            status TEXT NOT NULL DEFAULT 'queued' CHECK (
                status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')
            ),
            priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 9),
            required_role TEXT CHECK (
                required_role IS NULL OR required_role IN ('eph', 'target')
            ),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            result JSONB,
            idempotency_key TEXT NOT NULL UNIQUE,
            assigned_agent_id UUID REFERENCES agent_control_agents(agent_id),
            lease_token_hash TEXT,
            lease_expires_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute("""
        ALTER TABLE agent_control_work_items
        DROP CONSTRAINT IF EXISTS agent_control_work_items_kind_check
    """)
    connection.execute("""
        ALTER TABLE agent_control_work_items
        ADD CONSTRAINT agent_control_work_items_kind_check CHECK (
            kind IN (
                'deployment', 'maintenance', 'inspection', 'diagnostic', 'software'
            )
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_approvals (
            approval_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            work_id UUID NOT NULL REFERENCES agent_control_work_items(work_id)
                ON DELETE CASCADE,
            action TEXT NOT NULL CHECK (char_length(action) BETWEEN 1 AND 128),
            payload_digest TEXT NOT NULL CHECK (
                payload_digest ~ '^[a-f0-9]{64}$'
            ),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'approved', 'rejected', 'used', 'expired')
            ),
            requested_by TEXT NOT NULL CHECK (
                char_length(requested_by) BETWEEN 1 AND 128
            ),
            approved_by TEXT,
            reason TEXT,
            expires_at TIMESTAMPTZ NOT NULL,
            approved_at TIMESTAMPTZ,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_approvals_project_idx
        ON agent_control_approvals (project_id, status, created_at DESC)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_work_claim_idx
        ON agent_control_work_items (status, priority DESC, created_at)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_skills_project_idx
        ON agent_control_skills (project_id, enabled)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_plugins_project_idx
        ON agent_control_plugins (project_id, enabled)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_workflows (
            workflow_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'published' CHECK (
                status IN ('published', 'archived')
            ),
            latest_version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (project_id, name)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_workflow_versions (
            workflow_id UUID NOT NULL REFERENCES agent_control_workflows(workflow_id)
                ON DELETE CASCADE,
            version INTEGER NOT NULL,
            definition JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (workflow_id, version)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_workflow_runs (
            run_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL REFERENCES agent_control_workflows(workflow_id)
                ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running' CHECK (
                status IN ('running', 'succeeded', 'failed', 'cancelled')
            ),
            input JSONB NOT NULL DEFAULT '{}'::jsonb,
            output JSONB,
            idempotency_key TEXT NOT NULL UNIQUE,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_tasks (
            task_id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES agent_control_projects(project_id)
                ON DELETE CASCADE,
            workflow_run_id UUID REFERENCES agent_control_workflow_runs(run_id)
                ON DELETE CASCADE,
            step_id TEXT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued' CHECK (
                status IN ('blocked', 'queued', 'leased', 'succeeded', 'failed', 'cancelled')
            ),
            priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 9),
            required_role TEXT CHECK (
                required_role IS NULL OR required_role IN ('eph', 'target')
            ),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            depends_on JSONB NOT NULL DEFAULT '[]'::jsonb,
            result JSONB,
            idempotency_key TEXT NOT NULL UNIQUE,
            assigned_agent_id UUID REFERENCES agent_control_agents(agent_id),
            lease_token_hash TEXT,
            lease_expires_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (workflow_run_id, step_id)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_tasks_claim_idx
        ON agent_control_tasks (status, priority DESC, created_at)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_tasks_run_idx
        ON agent_control_tasks (workflow_run_id, status)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_workflow_runs_project_idx
        ON agent_control_workflow_runs (project_id, status)
    """)


def check_store(database_url: str) -> None:
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        connection.execute("SELECT 1").fetchone()


def _tls_settings() -> tuple[str, str, str]:
    cert_file = _required("CLOUD_CONTROL_TLS_CERT_FILE")
    key_file = _required("CLOUD_CONTROL_TLS_KEY_FILE")
    client_ca_file = _required("CLOUD_CONTROL_TLS_CLIENT_CA_FILE")
    for value in (cert_file, key_file, client_ca_file):
        if not Path(value).is_file():
            raise CloudControlConfigurationError("Cloud Control TLS file is missing")
    return cert_file, key_file, client_ca_file


def main() -> int:
    parser = argparse.ArgumentParser(prog="cloud-control-agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    parser.add_argument("--reconcile-once", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        print(json.dumps({"service": "cloud-control-agent", "status": "healthy"}))
        return 0
    try:
        database_url = _database_url()
        if args.readiness_check:
            check_store(database_url)
            return 0
        if args.reconcile_once:
            with psycopg.connect(database_url, autocommit=True) as connection:
                initialize_store(connection)
                summary = reconcile_deployments(connection)
            print(json.dumps({
                "service": "cloud-control-agent",
                "action": "reconcile",
                **summary,
            }), flush=True)
            return 0
        cert_file, key_file, client_ca_file = _tls_settings()
        host = os.environ.get("CLOUD_CONTROL_BIND_HOST", "0.0.0.0")
        port = int(os.environ.get("CLOUD_CONTROL_BIND_PORT", "8443"))
        if not 1 <= port <= 65535:
            raise CloudControlConfigurationError("CLOUD_CONTROL_BIND_PORT is invalid")
        with psycopg.connect(database_url, autocommit=True) as connection:
            initialize_store(connection)
        print(json.dumps({"service": "cloud-control-agent", "status": "ready"}), flush=True)
        serve(database_url, host, port, cert_file, key_file, client_ca_file)
    except (CloudControlConfigurationError, psycopg.Error, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
