"""Credential and dependency security checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .scanner import SKIP_DIRECTORIES

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
]
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".whl", ".ico"}


@dataclass
class SecretFinding:
    path: str
    line: int
    pattern: str

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "pattern": self.pattern}


def scan_secrets(root: Path) -> list[SecretFinding]:
    root = root.resolve()
    findings: list[SecretFinding] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(SecretFinding(str(relative), number, pattern.pattern))
                    break
    return findings


def parse_pip_audit(document: str | dict) -> list[dict]:
    """Normalise ``pip-audit --format json`` output into fixable vulnerabilities."""

    data = json.loads(document) if isinstance(document, str) else document
    results = []
    for dependency in data.get("dependencies", []) if isinstance(data, dict) else []:
        for vulnerability in dependency.get("vulns", []):
            results.append(
                {
                    "package": dependency.get("name"),
                    "version": dependency.get("version"),
                    "id": vulnerability.get("id"),
                    "fix_versions": vulnerability.get("fix_versions", []),
                    "description": vulnerability.get("description", ""),
                }
            )
    return results


def parse_npm_audit(document: str | dict) -> list[dict]:
    """Normalise ``npm audit --json`` output (npm 7+ format)."""

    data = json.loads(document) if isinstance(document, str) else document
    results = []
    vulnerabilities = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    for name, entry in vulnerabilities.items():
        if not isinstance(entry, dict):
            continue
        results.append(
            {
                "package": name,
                "severity": entry.get("severity"),
                "range": entry.get("range"),
                "fix_available": bool(entry.get("fixAvailable")),
                "via": entry.get("via", []),
            }
        )
    return results


def audit_commands(root: Path) -> list[list[str]]:
    commands = []
    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        commands.append(["pip-audit", "--format", "json"])
    if (root / "package.json").exists():
        commands.append(["npm", "audit", "--json"])
    return commands
