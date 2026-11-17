from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


class CloudControlConfigurationError(ValueError):
    pass


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise CloudControlConfigurationError("DATABASE_URL is required")
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
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_control_commands (
            command_id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES agent_control_deployments(deployment_id),
            target_agent_id UUID NOT NULL,
            required_role TEXT NOT NULL CHECK (required_role IN ('eph', 'target')),
            action TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload JSONB NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'succeeded', 'failed', 'blocked', 'cancelled')),
            lease_id UUID,
            lease_expires_at TIMESTAMPTZ,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS agent_control_commands_claim_idx
        ON agent_control_commands (required_role, target_agent_id, status, created_at)
    """)


def check_store(database_url: str) -> None:
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        connection.execute("SELECT 1").fetchone()


def serve_healthcheck() -> int:
    print(json.dumps({"service": "cloud-control-agent", "status": "healthy"}))
    return 0


def run() -> int:
    database_url = _database_url()
    with psycopg.connect(database_url, autocommit=True) as connection:
        initialize_store(connection)
        check_store(database_url)
        print(json.dumps({"service": "cloud-control-agent", "status": "ready"}), flush=True)
        running = True

        def stop_handler(_signum: int, _frame: Any) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        while running:
            connection.execute("SELECT 1").fetchone()
            time.sleep(5)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cloud-control-agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return serve_healthcheck()
    if args.readiness_check:
        try:
            check_store(_database_url())
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
