"""Read-only readiness report for the physical Metal3 lab workflow.

The lab workflow must never fake a bare-metal result, but it also must not fail
with an opaque red run when a secret or an input is missing. This tool judges
only what can be judged without touching the lab: workflow inputs, the shape of
the two kubeconfigs, and the presence of the acceptance command. It never
connects to a cluster, so a green readiness report is not an acceptance result.

Exit status is always 0 unless the report cannot be written; the decision is
carried by ``ready`` (and by the ``GITHUB_OUTPUT`` file when requested).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _kubeconfig_findings(label: str, path: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not path:
        findings.append(_check(f"{label}.present", False, "kubeconfig secret is empty"))
        return findings
    source = Path(path)
    if not source.is_file() or not source.read_text(encoding="utf-8").strip():
        findings.append(
            _check(f"{label}.present", False, "kubeconfig secret is empty")
        )
        return findings
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        findings.append(
            _check(f"{label}.parse", False, "kubeconfig is not valid YAML")
        )
        return findings
    if not isinstance(document, dict):
        findings.append(
            _check(f"{label}.parse", False, "kubeconfig is not a mapping")
        )
        return findings
    clusters = document.get("clusters")
    contexts = document.get("contexts")
    users = document.get("users")
    findings.append(
        _check(
            f"{label}.clusters",
            isinstance(clusters, list) and bool(clusters),
            "kubeconfig must define at least one cluster",
        )
    )
    findings.append(
        _check(
            f"{label}.contexts",
            isinstance(contexts, list) and bool(contexts),
            "kubeconfig must define at least one context",
        )
    )
    findings.append(
        _check(
            f"{label}.users",
            isinstance(users, list) and bool(users),
            "kubeconfig must define at least one user",
        )
    )
    server = None
    if isinstance(clusters, list) and clusters and isinstance(clusters[0], dict):
        cluster = clusters[0].get("cluster")
        if isinstance(cluster, dict):
            server = cluster.get("server")
    findings.append(
        _check(
            f"{label}.server",
            isinstance(server, str) and server.startswith(("https://", "http://")),
            "cluster entry must expose a server URL",
        )
    )
    authority = None
    if isinstance(clusters, list) and clusters and isinstance(clusters[0], dict):
        cluster = clusters[0].get("cluster")
        if isinstance(cluster, dict):
            authority = cluster.get("certificate-authority-data")
    if authority:
        try:
            base64.b64decode(str(authority), validate=True)
            decoded_ok = True
        except (ValueError, TypeError):
            decoded_ok = False
        findings.append(
            _check(
                f"{label}.ca",
                decoded_ok,
                "certificate-authority-data must be valid base64",
            )
        )
    return findings


def build_report(arguments: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "project_id",
            bool(UUID_PATTERN.match(str(arguments.project_id or "").strip()))
            and _is_uuid(arguments.project_id),
            "project id must be a UUID",
        )
    )
    control_plane = _positive_int(arguments.control_plane_replicas)
    worker = _positive_int(arguments.worker_replicas)
    checks.append(
        _check(
            "control_plane_replicas",
            control_plane is not None,
            "control-plane replica count must be a positive integer",
        )
    )
    checks.append(
        _check(
            "worker_replicas",
            worker is not None,
            "worker replica count must be a positive integer",
        )
    )
    checks.extend(_kubeconfig_findings("eph", arguments.eph_kubeconfig or ""))
    checks.extend(_kubeconfig_findings("target", arguments.target_kubeconfig or ""))
    checks.append(
        _check(
            "acceptance_tool",
            Path(arguments.acceptance_tool).is_file(),
            "acceptance tool must exist in the checkout",
        )
    )
    secret_presence = {}
    for label, path in (
        ("eph", arguments.eph_kubeconfig or ""),
        ("target", arguments.target_kubeconfig or ""),
    ):
        source = Path(path) if path else None
        # Only the presence is reported; the kubeconfig content is never echoed.
        client_present = bool(
            source is not None
            and source.is_file()
            and source.read_text(encoding="utf-8").strip()
        )
        secret_presence[label] = "configured" if client_present else "empty"
    ready = all(item["ok"] for item in checks)
    skipped = []
    if not ready:
        skipped = [
            "preflight (platform API inventory)",
            "redfish inventory",
            "deployment acceptance",
        ]
    return {
        "ready": ready,
        "secret_presence": secret_presence,
        "checks": checks,
        "failed_checks": [item["name"] for item in checks if not item["ok"]],
        "skipped_stages": skipped,
        "not_checked": [
            "lab runner reachability (only provable on the self-hosted runner)",
            "BMC reachability",
            "cluster state",
        ],
        "note": (
            "Readiness only. A green report means the workflow can attempt the "
            "acceptance; it is not evidence of a bare-metal deployment."
        ),
    }


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", dest="project_id", required=True)
    parser.add_argument("--control-plane-replicas", required=True)
    parser.add_argument("--worker-replicas", required=True)
    parser.add_argument("--eph-kubeconfig", default="")
    parser.add_argument("--target-kubeconfig", default="")
    parser.add_argument(
        "--acceptance-tool",
        default="tools/metal3_acceptance.py",
    )
    parser.add_argument("--out", default="evidence/lab-readiness.json")
    parser.add_argument("--github-output", default="")
    arguments = parser.parse_args(argv)

    report = build_report(arguments)
    output = Path(arguments.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    for item in report["checks"]:
        mark = "OK  " if item["ok"] else "FAIL"
        print(f"{mark} {item['name']}: {item['detail']}")
    if not report["ready"]:
        print(
            "Readiness failed: "
            + ", ".join(report["failed_checks"])
            + ". Acceptance stages will be skipped."
        )
    if arguments.github_output:
        with open(arguments.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"ready={'true' if report['ready'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
