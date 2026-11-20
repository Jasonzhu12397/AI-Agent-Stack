"""Deterministic multi-file patches with diffs and approval enforcement."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from .policy import ApprovalRequired, ApprovalStore, read_text, resolve_workspace_path, write_text


class PatchError(RuntimeError):
    """The patch cannot be applied unambiguously."""


@dataclass
class TextEdit:
    path: str
    old: str
    new: str

    def validate(self, root: Path) -> None:
        if not self.old:
            raise PatchError(f"{self.path}: old text must not be empty")
        source = read_text(root, self.path)
        occurrences = source.count(self.old)
        if occurrences != 1:
            raise PatchError(
                f"{self.path}: expected exactly one occurrence of the old text, found {occurrences}"
            )

    def render(self, root: Path) -> str:
        source = read_text(root, self.path)
        if source.count(self.old) != 1:
            raise PatchError(f"{self.path}: old text is no longer unique")
        return source.replace(self.old, self.new, 1)


def plan(edits: list[TextEdit], root: Path) -> str:
    """Validate every edit and return a unified diff for review."""

    if not edits:
        raise PatchError("at least one edit is required")
    chunks: list[str] = []
    for edit in edits:
        edit.validate(root)
        before = read_text(root, edit.path).splitlines(keepends=True)
        after = edit.render(root).splitlines(keepends=True)
        chunks.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{edit.path}",
                tofile=f"b/{edit.path}",
            )
        )
    return "".join(chunks)


def digest_payload(edits: list[TextEdit]) -> str:
    return "\n".join(f"{edit.path}\0{edit.old}\0{edit.new}" for edit in edits)


def apply(edits: list[TextEdit], root: Path, approvals: ApprovalStore, token: str) -> list[str]:
    """Apply a plan after consuming a one-time approval bound to its content."""

    try:
        approvals.consume(token, "apply-patch", digest_payload(edits))
    except ApprovalRequired as exc:
        raise ApprovalRequired(str(exc)) from exc
    written: list[str] = []
    for edit in edits:
        resolve_workspace_path(root, edit.path)
        content = edit.render(root)
        write_text(root, edit.path, content)
        written.append(edit.path)
    return written
