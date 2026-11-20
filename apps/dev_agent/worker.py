"""Autonomous software worker for EPH and Target agents.

EPH/Target agents perform production work without human approval. This worker
claims only ``software`` items, applies small bug fixes and runs the test suite.
It never pushes or deploys; repository scope is fixed by the mounted workspace.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path
from typing import Any

from packages.agent_transport.client import AgentControlClient, AgentTransportError
from packages.agent_transport.config import AgentSettings, ConfigurationError

from .work import serve

LOG_PREFIX = "dev-agent-worker"


def _log(message: str, **fields: Any) -> None:
    print(json.dumps({"service": "dev-agent-worker", "message": message, **fields}), flush=True)


def run(role: str, root: Path, once: bool = False, max_items: int = 1) -> int:
    try:
        settings = AgentSettings.from_environment(role)
    except ConfigurationError as error:
        _log("configuration_error", error=str(error))
        return 1
    client = AgentControlClient(settings)
    stopped = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    sequence = 0
    try:
        while not stopped:
            try:
                client.heartbeat(
                    sequence,
                    "ready",
                    ["software.workspace", "software.bugfix", "software.tests"],
                )
                handled = serve(
                    client,
                    root,
                    max_items=max_items,
                    autonomous=True,
                    actor=f"{role}-agent",
                )
                for item in handled:
                    _log(
                        "work_executed",
                        work_id=item.get("work_id"),
                        status=item.get("status"),
                        task_id=item.get("task_id"),
                    )
                sequence += 1
            except AgentTransportError as error:
                _log("transport_error", error=str(error))
            if once:
                break
            time.sleep(settings.poll_seconds)
    finally:
        client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dev-agent-worker")
    parser.add_argument("--role", choices=["eph", "target"], required=True)
    parser.add_argument(
        "--root",
        default="/workspace",
        help="mounted repository root; cannot be chosen by a work payload",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-items", type=int, default=1)
    arguments = parser.parse_args()
    return run(arguments.role, Path(arguments.root), arguments.once, arguments.max_items)


if __name__ == "__main__":
    raise SystemExit(main())
