#!/usr/bin/env python3
"""Import the upstream Metal3 dev-env tree into this repository without git.

The lab runner must never depend on git or on a moving upstream branch at run
time. This tool is the only supported way to materialise the upstream code:

* it reads a tarball/zip archive (local path or http(s) URL) for the pinned
  commit, extracts it into ``deploy/metal3-lab/vendor/metal3-dev-env``,
* writes ``VENDOR.json`` (provenance) and ``MANIFEST.sha256`` (integrity list),
* and can re-check the vendored tree later with ``--check``.

Everything it writes is ordinary source code that the repository owns: edit it
in place, review it in a diff, teach an agent to patch it. The upstream project
is referenced only as provenance, never as a runtime dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

PINNED_COMMIT = "7076f07624cdfba990f83bea24f749b37e3baad7"
ARCHIVE_URL = (
    "https://codeload.github.com/metal3-io/metal3-dev-env/tar.gz/" + PINNED_COMMIT
)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO_ROOT / "deploy" / "metal3-lab" / "vendor" / "metal3-dev-env"
MANIFEST_NAME = "MANIFEST.sha256"
META_NAME = "VENDOR.json"
EXCLUDED_TOP_LEVEL = {".git", ".github"}

# Files the lab runner executes must exist after an import; the check fails
# loudly when an archive is incomplete instead of failing halfway through a
# host-mutating prepare.
REQUIRED_PATHS = (
    "01_prepare_host.sh",
    "02_configure_host.sh",
    "03_launch_mgmt_cluster.sh",
    "04_verify.sh",
    "Makefile",
    "ansible.cfg",
    "lib/common.sh",
    "vm-setup/setup-playbook.yml",
    "vm-setup/inventory.ini",
)


class ImportError_(RuntimeError):
    pass


def _log(message: str) -> None:
    print(f"[vendor] {message}", flush=True)


def _download(url: str, destination: Path, timeout: int = 60) -> Path:
    _log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "ai-agent-stack-vendor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    return destination


def _safe_members(root: Path) -> list[Path]:
    """Return archive members that are safe to extract, rejecting traversal."""

    if root.suffix == ".zip":
        with zipfile.ZipFile(root) as archive:
            names = [entry.filename for entry in archive.infolist()]
    else:
        with tarfile.open(root) as archive:
            names = [member.name for member in archive.getmembers()]
    for name in names:
        parts = PurePosixPath(name).parts
        if not parts:
            continue
        if any(part in {"..", ""} for part in parts) or name.startswith("/"):
            raise ImportError_(f"archive member escapes the target directory: {name}")
    return names


def _extract(root: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if root.suffix == ".zip":
        with zipfile.ZipFile(root) as archive:
            for entry in archive.infolist():
                parts = PurePosixPath(entry.filename).parts
                if parts and parts[0] in EXCLUDED_TOP_LEVEL:
                    continue
                archive.extract(entry, destination)
        return
    with tarfile.open(root) as archive:

        def _keep(member, target_dir):
            parts = PurePosixPath(member.name).parts
            if parts and parts[0] in EXCLUDED_TOP_LEVEL:
                return None
            try:
                return tarfile.data_filter(member, target_dir)
            except AttributeError:  # pragma: no cover - older interpreters
                return member

        archive.extractall(destination, filter=_keep)


def _strip_archive_root(directory: Path) -> Path:
    """Flatten the single ``<repo>-<commit>`` directory produced by GitHub."""

    entries = [entry for entry in directory.iterdir() if entry.name not in EXCLUDED_TOP_LEVEL]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return directory


def _tree_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if relative.as_posix() in {MANIFEST_NAME, META_NAME}:
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(target: Path, commit: str, source: str) -> dict:
    files = _tree_files(target)
    lines = []
    for path in files:
        relative = path.relative_to(target).as_posix()
        lines.append(f"{_sha256(path)}  {relative}")
    manifest = target / MANIFEST_NAME
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    metadata = {
        "upstream_repo": "https://github.com/metal3-io/metal3-dev-env",
        "upstream_commit": commit,
        "imported_source": source,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "manifest_sha256": _sha256(manifest),
        "note": "Vendored source. Runtime never clones upstream; edit these files directly.",
    }
    (target / META_NAME).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def check_tree(target: Path, expected_commit: str | None = PINNED_COMMIT) -> dict:
    meta_path = target / META_NAME
    manifest_path = target / MANIFEST_NAME
    if not meta_path.exists() or not manifest_path.exists():
        raise ImportError_(
            f"{target} is not a vendored tree (missing {META_NAME} or {MANIFEST_NAME}); "
            "run tools/vendor_import.py first"
        )
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if expected_commit and metadata.get("upstream_commit") != expected_commit:
        raise ImportError_(
            "vendored commit "
            f"{metadata.get('upstream_commit')} does not match the pinned {expected_commit}"
        )
    if _sha256(manifest_path) != metadata.get("manifest_sha256"):
        raise ImportError_(
            "manifest digest does not match VENDOR.json; the tree was edited by hand"
        )
    for required in REQUIRED_PATHS:
        if not (target / required).exists():
            raise ImportError_(f"vendored tree is incomplete: missing {required}")
    problems = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        path = target / relative
        if not path.exists():
            problems.append(f"missing {relative}")
        elif _sha256(path) != digest:
            problems.append(f"modified {relative}")
    return {"metadata": metadata, "problems": problems}


def import_tree(source: str, target: Path = DEFAULT_TARGET, commit: str = PINNED_COMMIT) -> dict:
    target = target.resolve()
    if target.exists():
        shutil.rmtree(target)
    with tempfile.TemporaryDirectory() as workspace:
        archive = Path(workspace) / "upstream-archive"
        if source.startswith(("http://", "https://")):
            handle = _download(source, archive)
        else:
            local = Path(source).expanduser().resolve()
            if not local.exists():
                raise ImportError_(f"archive not found: {local}")
            handle = Path(workspace) / ("upstream" + local.suffix)
            shutil.copyfile(local, handle)
        _safe_members(handle)
        staging = Path(workspace) / "staging"
        _extract(handle, staging)
        root = _strip_archive_root(staging)
        for excluded in EXCLUDED_TOP_LEVEL:
            leftover = root / excluded
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, target, symlinks=False)
    metadata = write_manifest(target, commit, source)
    _log(
        f"vendored {metadata['file_count']} files ({metadata['total_bytes']} bytes) "
        f"from commit {commit} into {target}"
    )
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="tarball/zip path or http(s) URL of the pinned archive")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--commit", default=PINNED_COMMIT)
    parser.add_argument("--check", action="store_true", help="verify the vendored tree only")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the pinned archive URL and exit",
    )
    args = parser.parse_args(argv)

    if args.print_url:
        print(ARCHIVE_URL)
        return 0
    try:
        if args.check:
            result = check_tree(args.target, args.commit)
            metadata = result["metadata"]
            _log(
                f"vendored tree OK: commit {metadata['upstream_commit']}, "
                f"{metadata['file_count']} files"
            )
            for problem in result["problems"]:
                _log(f"local change: {problem}")
            return 0
        import_tree(args.source or ARCHIVE_URL, args.target, args.commit)
        return 0
    except ImportError_ as error:
        print(f"[vendor] ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
