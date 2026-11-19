from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

from .api import serve


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
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_commands_claim_idx
        ON agent_control_commands (required_role, target_agent_id, status, created_at)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_agents_last_seen_idx
        ON agent_control_agents (last_seen)
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
    args = parser.parse_args()
    if args.healthcheck:
        print(json.dumps({"service": "cloud-control-agent", "status": "healthy"}))
        return 0
    try:
        database_url = _database_url()
        if args.readiness_check:
            check_store(database_url)
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
