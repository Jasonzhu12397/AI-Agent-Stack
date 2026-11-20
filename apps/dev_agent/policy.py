"""Filesystem policy and two-step approval for high-risk actions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

PROTECTED_NAMES = {".git", ".hg", ".svn", ".env", "id_rsa", "id_ed25519"}
PROTECTED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
MAX_READ_BYTES = 400_000


class PolicyError(RuntimeError):
    """A path or action violates the workspace policy."""


class ApprovalRequired(PolicyError):
    """A write was attempted without a valid one-time approval token."""


def resolve_workspace_path(root: Path, candidate: str | Path) -> Path:
    root = root.resolve()
    raw = Path(candidate)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PolicyError(f"path escapes the project root: {candidate}") from exc
    if any(part in PROTECTED_NAMES for part in path.parts):
        raise PolicyError(f"path is protected by policy: {candidate}")
    if path.suffix.lower() in PROTECTED_SUFFIXES:
        raise PolicyError(f"credential-like file is protected: {candidate}")
    return path


def read_text(root: Path, relative: str | Path, maximum: int = MAX_READ_BYTES) -> str:
    path = resolve_workspace_path(root, relative)
    if not path.is_file():
        raise FileNotFoundError(f"file does not exist: {relative}")
    if path.stat().st_size > maximum:
        raise PolicyError(f"file exceeds the read budget: {relative}")
    return path.read_text(encoding="utf-8")


def write_text(root: Path, relative: str | Path, content: str) -> Path:
    path = resolve_workspace_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@dataclass
class Approval:
    token: str
    action: str
    digest: str
    created_at: float


class ApprovalStore:
    """Persist one-time approvals so a CLI invocation can be split in two steps."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = self.root / ".dev-agent"
        self.path = self.directory / "approvals.json"

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}

    def _save(self, document: dict[str, dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

    def request(self, action: str, payload: str) -> Approval:
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        token = secrets.token_hex(16)
        document = self._load()
        document[token] = {
            "action": action,
            "digest": digest,
            "created_at": time.time(),
            "used": False,
        }
        self._save(document)
        return Approval(token=token, action=action, digest=digest, created_at=time.time())

    def consume(self, token: str, action: str, payload: str) -> None:
        document = self._load()
        entry = document.get(token)
        if not isinstance(entry, dict) or entry.get("used"):
            raise ApprovalRequired("approval token is unknown or already used")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if entry.get("action") != action or entry.get("digest") != digest:
            raise ApprovalRequired("approval token does not match this action and payload")
        entry["used"] = True
        self._save(document)
