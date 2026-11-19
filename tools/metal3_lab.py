#!/usr/bin/env python3
"""Run the physical-machine Metal3 lab through the AI agent's python pipeline.

This replaces ``deploy/metal3-lab/run.sh``. There is no shell lifecycle wrapper:
planning, BMH rendering, target deployment and verification are python calls;
only the three host-bootstrap scripts and explicit argv-only host cleanup steps
are executed as operating-system actions.

Examples::

    python3 tools/metal3_lab.py plan --project <project-id>
    python3 tools/metal3_lab.py prepare --allow-host-mutation --write-back
    python3 tools/metal3_lab.py verify
    python3 tools/metal3_lab.py clean --allow-host-mutation
    python3 tools/metal3_lab.py reference --vendor-source ./metal3-dev-env.tar.gz
    python3 tools/metal3_lab.py reseal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "apps" / "platform-api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from apps.eph_agent import metal3_lab  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("plan", "prepare", "auto", "verify", "clean", "reference", "reseal"),
    )
    parser.add_argument("--cluster-name")
    parser.add_argument("--namespace", default="metal3")
    parser.add_argument("--control-plane-count", type=int)
    parser.add_argument("--project")
    parser.add_argument("--control-plane-endpoint")
    parser.add_argument("--image-url")
    parser.add_argument("--k8s-version")
    parser.add_argument("--data-template")
    parser.add_argument("--network-spec")
    parser.add_argument("--auto-network", action="store_true")
    parser.add_argument("--target-kubeconfig")
    parser.add_argument("--target-ready-timeout", type=int, default=1800)
    parser.add_argument("--network-ready-timeout", type=int, default=900)
    parser.add_argument("--deployment-id")
    parser.add_argument("--deployment-ready-timeout", type=int, default=3600)
    parser.add_argument("--django-settings", default="platform_api.settings")
    parser.add_argument("--hosts-dir")
    parser.add_argument("--work-dir")
    parser.add_argument("--vendor-dir")
    parser.add_argument("--vendor-source")
    parser.add_argument("--write-back", action="store_true")
    parser.add_argument("--allow-host-mutation", action="store_true")
    parser.add_argument("--apply-retries", type=int, default=10)
    parser.add_argument("--retry-interval", type=float, default=60.0)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--ironic-url")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = metal3_lab.settings_from_namespace(args)
        metal3_lab.validate_settings(settings)
        lab = metal3_lab.Metal3Lab(settings)
        if args.action == "plan":
            lab.plan()
        else:
            lab.run(args.action)
    except (metal3_lab.LabError, metal3_lab.vendor_import.ImportError_) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
