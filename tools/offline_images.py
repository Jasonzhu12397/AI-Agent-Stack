#!/usr/bin/env python3
"""Fetch and build the complete offline EPH/Agent image set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.eph_agent.offline_images import (  # noqa: E402
    OfflineImageError,
    build_offline,
    fetch_assets,
)

DEFAULT_MANIFEST = ROOT / "deploy" / "offline" / "images.json"
DEFAULT_ASSETS = ROOT / "deploy" / "offline" / "assets"
DEFAULT_OUTPUT = ROOT / "deploy" / "offline" / "output"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="action", required=True)
    for action in ("fetch", "build"):
        command = subcommands.add_parser(action)
        command.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        command.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS)
        if action == "fetch":
            command.add_argument("--lock", type=Path, default=DEFAULT_ASSETS / "lock.json")
        else:
            command.add_argument(
                "--offline", action="store_true", help="explicitly confirm no network use"
            )
            command.add_argument("--lock", type=Path, default=DEFAULT_ASSETS / "lock.json")
            command.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
            command.add_argument("--endpoint")
            command.add_argument("--bootstrap", type=Path, help="existing bootstrap JSON to update")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.action == "fetch":
            result = fetch_assets(arguments.manifest, arguments.assets_dir, arguments.lock)
        else:
            result = build_offline(
                ROOT,
                arguments.manifest,
                arguments.assets_dir,
                arguments.lock,
                arguments.output_dir,
                endpoint=arguments.endpoint,
                bootstrap_path=arguments.bootstrap,
            )
    except OfflineImageError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "action": arguments.action, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
