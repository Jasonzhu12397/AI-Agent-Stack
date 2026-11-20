"""Git inspection, commit message synthesis and CHANGELOG updates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .sandbox import CommandResult, run

CONFLICT_START = re.compile(r"^<<<<<<< ")
CONFLICT_END = re.compile(r"^>>>>>>> ")

TYPE_RULES = [
    (re.compile(r"(^|/)(tests?|spec)/|(^|/)test_.*\.py$|\.test\.[jt]s$"), "test"),
    (re.compile(r"(^|/)(docs?|README|CHANGELOG)"), "docs"),
    (re.compile(r"(^|/)(Dockerfile|docker-compose|\.github/|deploy/|charts?/)"), "build"),
    (re.compile(r"(^|/)(package-lock\.json|poetry\.lock|requirements)"), "chore"),
    (re.compile(r"\.(ya?ml|json|toml|ini)$"), "chore"),
]


@dataclass
class GitStatus:
    branch: str
    changed: list[str]
    staged: list[str]
    untracked: list[str]
    clean: bool


def git(root: Path, *arguments: str, timeout: int = 120) -> CommandResult:
    return run(["git", *arguments], root, timeout=timeout)


def status(root: Path) -> GitStatus:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    current = branch.stdout.strip() if branch.ok else "unknown"
    raw = git(root, "status", "--porcelain")
    changed: list[str] = []
    staged: list[str] = []
    untracked: list[str] = []
    for line in raw.stdout.splitlines():
        if len(line) < 4:
            continue
        marker, path = line[:2], line[3:].strip()
        if "?" in marker:
            untracked.append(path)
        else:
            changed.append(path)
            if marker[0] not in {" ", "?"}:
                staged.append(path)
    return GitStatus(current, changed, staged, untracked, not (changed or untracked))


def diff(root: Path, staged: bool = False) -> str:
    result = git(root, "diff", *(["--cached"] if staged else []))
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def conflicted_files(root: Path) -> list[str]:
    raw = git(root, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in raw.stdout.splitlines() if line.strip()]


def has_conflict_markers(root: Path, paths: list[str] | None = None) -> list[str]:
    conflicted: list[str] = []
    candidates = paths or [
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    ]
    for candidate in candidates:
        path = root / candidate
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(CONFLICT_START.match(line) for line in text.splitlines()) and any(
            CONFLICT_END.match(line) for line in text.splitlines()
        ):
            conflicted.append(candidate)
    return conflicted


def _commit_type(paths: list[str]) -> str:
    for pattern, kind in TYPE_RULES:
        if all(pattern.search(path) for path in paths):
            return kind
    return "feat"


def _scope(paths: list[str]) -> str:
    roots = {path.split("/")[0] for path in paths}
    if len(roots) == 1:
        return roots.pop()
    return "repo"


def commit_message(paths: list[str], summary: str | None = None, body: str | None = None) -> str:
    if not paths:
        return "chore: no tracked change"
    kind = _commit_type(paths)
    subject = summary or f"update {len(paths)} file{'s' if len(paths) != 1 else ''}"
    subject = subject.strip().rstrip(".")
    header = f"{kind}({_scope(paths)}): {subject[:72]}"
    details = body.splitlines() if body else [f"- {path}" for path in paths[:12]]
    return "\n".join([header, "", "Changed:", *[f"  {line}" for line in details]])


def changelog_entry(version: str, changes: list[str], released: date | None = None) -> str:
    day = (released or date.today()).isoformat()
    lines = [f"## {version} - {day}", ""]
    lines.extend(f"- {change}" for change in changes)
    return "\n".join(lines) + "\n"


def append_changelog(root: Path, entry: str, filename: str = "CHANGELOG.md") -> Path:
    path = root / filename
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Changelog\n\n"
    path.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")
    return path


def create_branch(root: Path, name: str) -> CommandResult:
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", name) or name.startswith("-"):
        raise ValueError("invalid branch name")
    return git(root, "switch", "-c", name)


def stage_and_commit(root: Path, message: str, paths: list[str]) -> CommandResult:
    added = git(root, "add", "--", *paths)
    if not added.ok:
        return added
    return git(root, "commit", "-m", message)
