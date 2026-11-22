"""Build a fail-closed release manifest from Metal3 acceptance evidence.

The collector in :mod:`tools.metal3_acceptance` only reads state. This tool is
the release gate: it refuses to produce a passing manifest unless every
required evidence kind exists, parses as JSON, reports ``status == "pass"`` and
has no failing checks. It also hashes every evidence file and records the git
commit so an acceptance result can be attached to a release without trusting
mutable files on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.version import project_version

MANIFEST_VERSION = "1"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"

DEFAULT_REQUIRED_KINDS = (
    "metal3-preflight",
    "metal3-redfish-evidence",
    "metal3-cluster-evidence",
)
ALLOWED_KINDS = set(DEFAULT_REQUIRED_KINDS) | {"metal3-acceptance"}


class ReleaseEvidenceError(ValueError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else None


def _check_failures(checks: Any) -> list[str]:
    if not isinstance(checks, list):
        return ["checks must be an array"]
    failures = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            failures.append(f"check[{index}] must be an object")
            continue
        if check.get("status") == STATUS_FAIL:
            name = check.get("name", f"check[{index}]")
            detail = check.get("detail", "")
            failures.append(f"{name}: {detail}".strip())
    return failures


def load_evidence(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReleaseEvidenceError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseEvidenceError(f"{path} must contain a JSON object")
    kind = payload.get("kind")
    if kind not in ALLOWED_KINDS:
        raise ReleaseEvidenceError(f"{path} has unsupported evidence kind: {kind!r}")
    if payload.get("status") not in {STATUS_PASS, STATUS_FAIL}:
        raise ReleaseEvidenceError(f"{path} must report status pass or fail")
    return {
        "path": str(path),
        "name": path.name,
        "kind": kind,
        "version": payload.get("version"),
        "generated_at": payload.get("generated_at"),
        "status": payload["status"],
        "sha256": _sha256(data),
        "bytes": len(data),
        "checks": len(payload.get("checks", [])) if isinstance(payload.get("checks"), list) else 0,
        "failures": _check_failures(payload.get("checks", [])),
    }


def build_manifest(
    evidence_dir: Path,
    *,
    required_kinds: tuple[str, ...] = DEFAULT_REQUIRED_KINDS,
    commit: str | None = None,
) -> dict[str, Any]:
    if not evidence_dir.is_dir():
        raise ReleaseEvidenceError(f"evidence directory does not exist: {evidence_dir}")
    unknown = [kind for kind in required_kinds if kind not in ALLOWED_KINDS]
    if unknown:
        raise ReleaseEvidenceError(f"unsupported required kinds: {', '.join(unknown)}")
    evidence = []
    problems = []
    for path in sorted(evidence_dir.glob("*.json")):
        if path.name.endswith("release-manifest.json"):
            continue
        try:
            evidence.append(load_evidence(path))
        except ReleaseEvidenceError as exc:
            problems.append(str(exc))
    present_kinds = {item["kind"] for item in evidence}
    for kind in required_kinds:
        if kind not in present_kinds:
            problems.append(f"missing required evidence kind: {kind}")
    for item in evidence:
        if item["status"] != STATUS_PASS:
            problems.append(f"{item['name']} reports status fail")
        problems.extend(f"{item['name']}: {failure}" for failure in item["failures"])
    status = STATUS_PASS if not problems else STATUS_FAIL
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        # A release manifest has to name the product it seals, or two releases
        # whose evidence directory looks alike cannot be told apart.
        "product_version": project_version(),
        "commit": commit,
        "status": status,
        "required_kinds": list(required_kinds),
        "evidence": evidence,
        "problems": problems,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--out", type=Path, default=Path("evidence/release-manifest.json"))
    parser.add_argument(
        "--require",
        action="append",
        choices=sorted(ALLOWED_KINDS),
        help="Required evidence kind; repeat for multiple kinds.",
    )
    parser.add_argument("--commit", help="Override the detected git commit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    required = tuple(args.require) if args.require else DEFAULT_REQUIRED_KINDS
    try:
        manifest = build_manifest(
            args.evidence_dir,
            required_kinds=required,
            commit=args.commit or _git_commit(),
        )
    except ReleaseEvidenceError as exc:
        print(json.dumps({"status": STATUS_FAIL, "detail": str(exc)}, indent=2))
        return 1
    write_manifest(args.out, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["status"] == STATUS_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
