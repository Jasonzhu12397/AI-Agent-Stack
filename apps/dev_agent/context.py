"""Persistent project memory and the AGENTS.md contract file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import ProjectIndex, scan_project


@dataclass
class ProjectMemory:
    technology: dict
    conventions: dict
    modules: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "technology": self.technology,
            "conventions": self.conventions,
            "modules": self.modules,
            "entrypoints": self.entrypoints,
        }


def build_memory(index: ProjectIndex) -> ProjectMemory:
    conventions = {
        "line_length": 100,
        "test_command": "python -m pytest -q",
        "style": "ruff",
    }
    pyproject = index.root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"line-length\s*=\s*(\d+)", text)
        if match:
            conventions["line_length"] = int(match.group(1))
    package = index.root / "package.json"
    if package.exists():
        conventions["test_command"] = "npm test"
        conventions["style"] = "eslint"
    if (index.root / "go.mod").exists():
        conventions["test_command"] = "go test ./..."
        conventions["style"] = "gofmt"
    return ProjectMemory(
        technology=index.technology,
        conventions=conventions,
        modules=sorted(index.modules),
        entrypoints=index.entrypoints,
    )


def save_memory(root: Path, memory: ProjectMemory) -> Path:
    path = root.resolve() / ".dev-agent" / "memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_memory(root: Path) -> ProjectMemory | None:
    path = root.resolve() / ".dev-agent" / "memory.json"
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return ProjectMemory(
        technology=document.get("technology", {}),
        conventions=document.get("conventions", {}),
        modules=document.get("modules", []),
        entrypoints=document.get("entrypoints", []),
    )


def refresh_memory(root: Path) -> ProjectMemory:
    index = scan_project(root)
    memory = build_memory(index)
    save_memory(root, memory)
    return memory


def render_agents_md(memory: ProjectMemory) -> str:
    languages = ", ".join(memory.technology.get("languages", [])) or "unknown"
    frameworks = ", ".join(memory.technology.get("frameworks", [])) or "none detected"
    modules = "\n".join(f"- `{module}`" for module in memory.modules[:30]) or "- none"
    return f"""# AGENTS.md

This file is the project memory for the software engineering agent. Keep it
accurate; the agent reads it before planning changes.

## Stack
- Languages: {languages}
- Frameworks: {frameworks}
- Entrypoints: {", ".join(memory.entrypoints) or "none"}

## Commands
- Test: `{memory.conventions.get("test_command")}`
- Style: `{memory.conventions.get("style")}`
- Line length: {memory.conventions.get("line_length")}

## Modules
{modules}

## Guardrails
- Do not commit credentials, tokens or private keys.
- Every write requires an explicit approval token bound to the diff.
- Run tests before proposing a commit; failures must be repaired or reported.
- Never push or deploy without a human decision.
"""


def write_agents_md(root: Path, memory: ProjectMemory) -> Path:
    path = root.resolve() / "AGENTS.md"
    path.write_text(render_agents_md(memory), encoding="utf-8")
    return path
