"""Run the entire Metal3 physical acceptance pipeline in one command.

This is the automatic release path: it executes every read-only evidence stage
in order, writes the stage JSON into an evidence directory, builds the aggregate
verification report and seals a fail-closed release manifest. Operators do not
assemble or merge reports manually.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_CLI = ROOT / "tools" / "metal3_acceptance.py"
MANIFEST_TOOL = "metal3_release_evidence"

from tools.metal3_release_evidence import (  # noqa: E402
    STATUS_FAIL,
    STATUS_PASS,
    build_manifest,
    write_manifest,
)


class ReleaseRunError(ValueError):
    pass


def build_stage_commands(
    args: argparse.Namespace, evidence_dir: Path
) -> list[tuple[str, list[str]]]:
    global_options = ["--django-settings", args.django_settings]
    project = ["--project", args.project]
    cluster = [
        "--kubeconfig",
        str(args.kubeconfig),
        "--control-plane-replicas",
        str(args.control_plane_replicas),
        "--worker-replicas",
        str(args.worker_replicas),
    ]
    if args.target_kubeconfig:
        cluster.extend(["--target-kubeconfig", str(args.target_kubeconfig)])
    stages: list[tuple[str, list[str]]] = [
        (
            "preflight",
            [
                *global_options,
                "preflight",
                *project,
                "--out",
                str(evidence_dir / "01-preflight.json"),
            ],
        )
    ]
    if not args.skip_redfish:
        stages.append(
            (
                "redfish",
                [
                    *global_options,
                    "redfish",
                    *project,
                    "--timeout",
                    str(args.timeout),
                    "--out",
                    str(evidence_dir / "02-redfish.json"),
                ],
            )
        )
    stages.append(
        (
            "cluster",
            [
                *global_options,
                "cluster",
                *cluster,
                "--out",
                str(evidence_dir / "03-cluster.json"),
            ],
        )
    )
    verify_command = [*global_options, "verify", *project, *cluster]
    if not args.skip_redfish:
        verify_command.extend(["--redfish", "--timeout", str(args.timeout)])
    verify_command.extend(["--out", str(evidence_dir / "04-acceptance.json")])
    stages.append(("verify", verify_command))
    return stages


def run_pipeline(
    args: argparse.Namespace,
    *,
    runner=subprocess.run,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evidence_dir = args.evidence_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)
    commands = build_stage_commands(args, evidence_dir)
    stages: list[dict[str, Any]] = []
    for index, (name, command) in enumerate(commands, start=1):
        argv = [sys.executable, str(ACCEPTANCE_CLI), *command]
        log_path = evidence_dir / f"{index:02d}-{name}.log"
        try:
            completed = runner(
                argv,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=args.stage_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            stages.append(
                {
                    "stage": name,
                    "status": STATUS_FAIL,
                    "returncode": None,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        log_path.write_text(
            f"argv={json.dumps(argv)}\nreturncode={completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}\n",
            encoding="utf-8",
        )
        stages.append(
            {
                "stage": name,
                "status": STATUS_PASS if completed.returncode == 0 else STATUS_FAIL,
                "returncode": completed.returncode,
                "log": str(log_path),
            }
        )
    required = ["metal3-preflight", "metal3-cluster-evidence", "metal3-acceptance"]
    if not args.skip_redfish:
        required.insert(1, "metal3-redfish-evidence")
    manifest = build_manifest(
        evidence_dir,
        required_kinds=tuple(required),
        commit=args.commit,
    )
    stage_failed = any(stage["status"] != STATUS_PASS for stage in stages)
    if stage_failed and manifest["status"] == STATUS_PASS:
        manifest["status"] = STATUS_FAIL
        manifest["problems"] = [
            *manifest["problems"],
            "one or more acceptance stages returned a non-zero exit code",
        ]
    manifest["stages"] = stages
    manifest["runner_version"] = MANIFEST_TOOL
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    return manifest, stages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--target-kubeconfig", type=Path)
    parser.add_argument("--control-plane-replicas", type=int, default=3)
    parser.add_argument("--worker-replicas", type=int, default=0)
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--out", type=Path, default=Path("evidence/release-manifest.json"))
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--stage-timeout", type=int, default=900)
    parser.add_argument("--skip-redfish", action="store_true")
    parser.add_argument("--commit", help="Override the detected git commit.")
    parser.add_argument("--django-settings", default="platform_api.settings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.timeout <= 120:
        raise SystemExit("--timeout must be between 1 and 120 seconds")
    if args.control_plane_replicas < 1 or args.worker_replicas < 0:
        raise SystemExit("replica counts must be positive")
    if not args.kubeconfig.exists():
        print(json.dumps({"status": STATUS_FAIL, "detail": "kubeconfig not found"}, indent=2))
        return 1
    try:
        manifest, _ = run_pipeline(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": STATUS_FAIL, "detail": str(exc)}, indent=2))
        return 1
    write_manifest(args.out, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["status"] == STATUS_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
