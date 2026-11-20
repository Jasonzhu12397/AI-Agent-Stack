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
APPROVAL_TTL_SECONDS = 86_400
APPROVAL_STATUSES = ("pending", "approved", "used")


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
    status: str = "pending"
    expires_at: float = 0.0
    approved_by: str = ""


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
        created = time.time()
        entry = {
            "action": action,
            "digest": digest,
            "created_at": created,
            "expires_at": created + APPROVAL_TTL_SECONDS,
            "status": "pending",
            "approved_at": None,
            "approved_by": "",
            "used": False,
        }
        document = self._load()
        document[token] = entry
        self._save(document)
        return Approval(
            token=token,
            action=action,
            digest=digest,
            created_at=created,
            status="pending",
            expires_at=entry["expires_at"],
        )

    def approve(self, token: str, approver: str = "operator") -> Approval:
        """Mark a pending plan as approved; only a human/host operator should call this."""
        return self._transition(token, "approved", approver)

    def _transition(self, token: str, status: str, approver: str = "") -> Approval:
        document = self._load()
        entry = document.get(token)
        if not isinstance(entry, dict):
            raise ApprovalRequired("approval token is unknown")
        current = entry.get("status") or ("used" if entry.get("used") else "pending")
        if current == "used":
            raise ApprovalRequired("approval token was already used")
        if float(entry.get("expires_at", 0)) and time.time() > float(entry["expires_at"]):
            raise ApprovalRequired("approval token expired")
        if current == "approved" and status == "approved":
            return Approval(
                token=token,
                action=str(entry["action"]),
                digest=str(entry["digest"]),
                created_at=float(entry["created_at"]),
                status="approved",
                expires_at=float(entry.get("expires_at", 0)),
                approved_by=str(entry.get("approved_by", "")),
            )
        if current != "pending":
            raise ApprovalRequired(f"approval token is {current}")
        entry["status"] = status
        entry["approved_at"] = time.time()
        entry["approved_by"] = approver
        entry["used"] = status == "used"
        self._save(document)
        return Approval(
            token=token,
            action=str(entry["action"]),
            digest=str(entry["digest"]),
            created_at=float(entry["created_at"]),
            status=status,
            expires_at=float(entry.get("expires_at", 0)),
            approved_by=approver,
        )

    def entries(self) -> dict[str, dict]:
        return self._load()

    def consume(self, token: str, action: str, payload: str) -> None:
        document = self._load()
        entry = document.get(token)
        if not isinstance(entry, dict):
            raise ApprovalRequired("approval token is unknown")
        status = entry.get("status") or ("used" if entry.get("used") else "pending")
        if status == "used":
            raise ApprovalRequired("approval token is unknown or already used")
        if status != "approved":
            raise ApprovalRequired("approval token is pending; run dev-agent approve first")
        if float(entry.get("expires_at", 0)) and time.time() > float(entry["expires_at"]):
            raise ApprovalRequired("approval token expired")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if entry.get("action") != action or entry.get("digest") != digest:
            raise ApprovalRequired("approval token does not match this action and payload")
        entry["status"] = "used"
        entry["used"] = True
        entry["used_at"] = time.time()
        self._save(document)
