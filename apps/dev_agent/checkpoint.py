"""Durable task checkpoints so long-running engineering work can resume."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PHASES = ("analyse", "change", "review", "test", "debug", "commit", "deliver")
STATUSES = ("running", "awaiting-approval", "succeeded", "failed", "cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TaskRecord:
    task_id: str
    goal: str
    phase: str = "analyse"
    status: str = "running"
    checkpoint: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class TaskStore:
    """JSON-backed task journal, safe for a single CLI process at a time."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = self.root / ".dev-agent"
        self.path = self.directory / "tasks.json"

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
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def create(self, goal: str) -> TaskRecord:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal must not be empty")
        task_id = f"task-{secrets.token_hex(6)}"
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            created_at=_now(),
            updated_at=_now(),
        )
        document = self._load()
        document[task_id] = record.as_dict()
        self._save(document)
        return record

    def get(self, task_id: str) -> TaskRecord:
        document = self._load()
        if task_id not in document:
            raise KeyError(f"unknown task: {task_id}")
        return TaskRecord(**document[task_id])

    def list(self, status: str | None = None) -> list[TaskRecord]:
        records = [TaskRecord(**item) for item in self._load().values()]
        if status:
            records = [record for record in records if record.status == status]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def update(
        self,
        task_id: str,
        *,
        phase: str | None = None,
        status: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
    ) -> TaskRecord:
        if phase is not None and phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        document = self._load()
        if task_id not in document:
            raise KeyError(f"unknown task: {task_id}")
        record = document[task_id]
        if phase is not None:
            record["phase"] = phase
        if status is not None:
            record["status"] = status
        if checkpoint is not None:
            record["checkpoint"] = checkpoint
        if artifacts is not None:
            record["artifacts"] = artifacts
        record["updated_at"] = _now()
        self._save(document)
        return TaskRecord(**record)

    def resume(self, task_id: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status not in {"running", "awaiting-approval"}:
            raise ValueError(f"task {task_id} is {record.status} and cannot be resumed")
        record.status = "running"
        record.checkpoint = {**record.checkpoint, "resumed_at": _now()}
        return self.update(task_id, status="running", checkpoint=record.checkpoint)
