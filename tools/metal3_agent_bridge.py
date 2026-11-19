#!/usr/bin/env python3
"""Hand Metal3 BMH work to the project's own agent skill.

The lab scripts must not act on the cluster themselves: registering bare metal
hosts and judging whether they became ready is agent work. This bridge is the
only seam the shell scripts touch - it loads the manifests, then calls
:mod:`apps.eph_agent.metal3_skill` so the agent performs the apply and the
observe/wait judgement.

Two commands:

* ``apply``  - create the BMC credential Secrets, then let the agent apply the
  BareMetalHost bundle (server-side apply, agent field manager).
* ``verify`` - let the agent observe the BMHs and decide whether every expected
  machine is present, reachable (``operationalStatus=OK``) and past
  introspection (provisioning state ready/available/provisioned).

The Kubernetes runner is injected here, exactly like in the agent process: the
skill itself never spawns kubectl.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.eph_agent import metal3_skill  # noqa: E402

MAX_OUTPUT_BYTES = 4 * 1024 * 1024
READY_STATES = {"available", "ready", "provisioned"}


def kubectl_runner(
    arguments: list[str],
    timeout: int = 60,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Minimal runner satisfying the agent's runner contract."""

    try:
        completed = subprocess.run(
            ["kubectl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-max_output_bytes:],
        "stderr": completed.stderr[-4000:],
    }


def load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"ERROR: manifest file not found: {path}")
    documents = [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]
    return documents


def split_bundle(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    secrets = [item for item in documents if item.get("kind") == "Secret"]
    hosts = [item for item in documents if item.get("kind") == metal3_skill.BMH_KIND]
    if not hosts:
        raise SystemExit("ERROR: the bundle contains no BareMetalHost objects")
    return secrets, hosts


class _StdinKubectl:
    """Pipe manifests to kubectl so nothing is left on disk."""

    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def __call__(self, arguments: list[str], timeout: int = 60, **_: Any) -> dict[str, Any]:
        payload = yaml.safe_dump_all(self.documents, sort_keys=False)
        try:
            completed = subprocess.run(
                ["kubectl", *arguments],
                check=False,
                capture_output=True,
                text=True,
                input=payload,
                timeout=timeout,
                env={**os.environ},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "error": type(exc).__name__}
        return {
            "available": True,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-MAX_OUTPUT_BYTES:],
            "stderr": completed.stderr[-4000:],
        }


def command_apply(args: argparse.Namespace) -> int:
    documents = load_documents(args.manifest)
    secrets, hosts = split_bundle(documents)

    for secret in secrets:
        runner = _StdinKubectl([secret])
        result = runner(
            ["apply", "--server-side", "-f", "-", "--namespace", args.namespace], timeout=60
        )
        if result.get("available") is not True or result.get("returncode") != 0:
            name = secret.get("metadata", {}).get("name")
            sys.stderr.write(
                f"ERROR: failed to apply BMC credential Secret {name}: {result.get('stderr', '')}\n"
            )
            return 1

    result = metal3_skill.apply_baremetalhosts(
        {"namespace": args.namespace, "manifests": hosts},
        _StdinKubectl(hosts),
        None,
    )
    applied = result.get("server_side_apply_succeeded")
    print(
        f"agent applied {len(hosts)} BareMetalHost object(s) "
        f"in namespace {args.namespace} (server_side_apply={applied})"
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    observation = metal3_skill.observe_baremetalhosts(
        {"namespace": args.namespace},
        kubectl_runner,
    )
    hosts = observation.get("hosts", [])
    failures: list[str] = []

    if args.expected is not None and observation.get("host_count") != args.expected:
        seen = observation.get("host_count")
        failures.append(f"expected {args.expected} BareMetalHost(s), the agent sees {seen}")

    for host in hosts:
        name = host.get("name")
        state = host.get("provisioning_state")
        status = host.get("operational_status")
        error = host.get("error_message") or ""
        ok = status == "OK" and state in READY_STATES and not error
        print(
            f"{'OK  ' if ok else 'FAIL'} {name}: operationalStatus={status} provisioning={state}"
            + (f" error={error[:120]}" if error else "")
        )
        if not ok:
            if status != "OK":
                failures.append(f"{name} operationalStatus is {status}, expected OK")
            if state not in READY_STATES:
                failures.append(
                    f"{name} provisioning state is {state}, expected one of {sorted(READY_STATES)}"
                )
            if error:
                failures.append(f"{name} reports {error[:200]}")

    if args.json:
        print(json.dumps(observation, indent=2))

    for failure in failures:
        sys.stderr.write(f"ERROR: {failure}\n")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Metal3 BMH apply/verify through the project's agent skill.",
    )
    parser.add_argument(
        "command",
        choices=("apply", "verify"),
        help="apply hands the BMH bundle to the agent; verify asks the agent to judge readiness",
    )
    parser.add_argument(
        "--manifest", type=Path, help="combined BMH manifest written by bmh_from_inventory.py"
    )
    parser.add_argument(
        "--namespace", default=None, help="namespace holding the BMHs (default: metal3)"
    )
    parser.add_argument(
        "--expected", type=int, default=None, help="number of machines the inventory promised"
    )
    parser.add_argument("--json", action="store_true", help="dump the agent observation as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.namespace = args.namespace or os.environ.get("EPH_CAPI_NAMESPACE", "metal3")
    os.environ["EPH_CAPI_NAMESPACE"] = args.namespace

    if args.command == "apply" and args.manifest is None:
        parser.error("apply requires --manifest")
    if args.command == "apply":
        return command_apply(args)
    return command_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
