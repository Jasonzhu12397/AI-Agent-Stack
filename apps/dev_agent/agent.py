"""Orchestrator for the software engineering agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoint import TaskStore
from .collaboration import default_tasks, run_roles
from .context import ProjectMemory, refresh_memory, write_agents_md
from .deployment import write_manifests
from .docs import docstring_edits, write_docs
from .gitops import commit_message, diff, status
from .openapi import load_spec
from .patching import TextEdit, apply, digest_payload, plan
from .performance import analyse_performance, summarise
from .planner import OpenAICompatiblePlanner, PlannerConfig, describe_plan
from .policy import ApprovalStore, read_text
from .roles import AnalystRole, DebugRole, DeployRole, ReviewRole, SecurityRole, analyse
from .scanner import ProjectIndex, scan_project
from .testing import TestReport, repair_loop, run_tests


@dataclass
class ChangePlan:
    diff: str
    token: str
    paths: list[str]


CAPABILITIES = {
    "code_understanding": "implemented: static index, outlines and call chains",
    "code_editing": "implemented: deterministic multi-file patches with approval",
    "code_review": "implemented: AST rules with severity, suggestion and diff",
    "tests": "implemented: discovery, execution, coverage parsing, bounded repair loop",
    "debugging": "implemented: traceback and gdb frame analysis",
    "git": "implemented: status, diff, commit message, changelog, branch, conflict scan",
    "security": "implemented: secret scan and pip/npm audit parsing",
    "openapi": "implemented: clients for Python and TypeScript plus mock examples",
    "deployment": "implemented: Dockerfile/K8s/CI generation, pre-flight, rollback plan",
    "docs": "implemented: README, architecture and API docs from static analysis",
    "comments": "implemented: approval-gated docstring insertion for undocumented functions",
    "performance": "implemented: static N+1, loop nesting, blocking sleep and query-shape rules",
    "pr_review": "implemented: tools/dev_agent_pr_review.py posts a single updated PR comment",
    "sandbox": "implemented: allowlisted, secret-scrubbed, timeout-bounded runner",
    "checkpoint": "implemented: durable task journal and resume",
    "model_planning": "implemented: OpenAI-compatible adapter with schema-validated, bounded plans",
    "collaboration": "implemented: concurrent analyst/reviewer/security/performance/tester roles",
}


class SoftwareAgent:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.store = TaskStore(self.root)
        self.approvals = ApprovalStore(self.root)
        self._index: ProjectIndex | None = None

    @property
    def index(self) -> ProjectIndex:
        if self._index is None:
            self._index = scan_project(self.root)
        return self._index

    def refresh(self) -> ProjectMemory:
        self._index = None
        return refresh_memory(self.root)

    def capabilities(self) -> dict[str, str]:
        return dict(CAPABILITIES)

    def analyse(self, target: str | None = None) -> dict:
        return analyse(self.root, target).data

    def review(self, paths: list[str] | None = None) -> dict:
        return ReviewRole().run(self.root, paths).data

    def security(self) -> dict:
        return SecurityRole().run(self.root).data

    def performance(self, paths: list[str] | None = None) -> dict:
        return summarise(analyse_performance(self.root, paths))

    def collaborate(self, paths: list[str] | None = None) -> dict:
        return run_roles(default_tasks(self.root, paths)).as_dict()

    def plan(self, goal: str, context: str | None = None, planner=None) -> dict:
        if planner is None:
            config = PlannerConfig.from_env()
            if config is None:
                raise RuntimeError(
                    "set DEV_AGENT_LLM_BASE_URL and DEV_AGENT_LLM_MODEL to enable planning"
                    " (or pass a planner instance)"
                )
            planner = OpenAICompatiblePlanner(config)
        context = context if context is not None else self.index.outline()
        return describe_plan(planner.plan(goal, context))

    def comment_plan(self, limit: int = 20) -> ChangePlan:
        return self.plan_change(docstring_edits(self.index, limit=limit))

    def tests(self, coverage: bool = False) -> TestReport:
        return run_tests(self.root, coverage=coverage)

    def repair_tests(self, repair: Callable[[TestReport], bool], max_attempts: int = 3) -> dict:
        outcome = repair_loop(self.root, repair, max_attempts=max_attempts)
        return {
            "status": outcome.status,
            "attempts": outcome.attempts,
            "history": outcome.history,
            "report": outcome.report.as_dict() if outcome.report else None,
        }

    def debug(self, log: str) -> dict:
        return DebugRole().run(log).data

    def preflight(
        self, nodes: list[dict], cpu_millicores: int = 100, memory_mib: int = 256
    ) -> dict:
        return DeployRole().run(nodes, cpu_millicores, memory_mib).data

    def read(self, relative: str) -> str:
        return read_text(self.root, relative)

    def plan_change(self, edits: list[TextEdit]) -> ChangePlan:
        rendered = plan(edits, self.root)
        token = self.approvals.request("apply-patch", digest_payload(edits)).token
        return ChangePlan(rendered, token, [edit.path for edit in edits])

    def apply_change(self, edits: list[TextEdit], token: str) -> list[str]:
        return apply(edits, self.root, self.approvals, token)

    def git_status(self) -> dict[str, Any]:
        current = status(self.root)
        return current.__dict__ | {"diff": diff(self.root)}

    def commit_plan(self, paths: list[str] | None = None, summary: str | None = None) -> str:
        changed = paths or status(self.root).changed
        return commit_message(changed, summary)

    def documents(
        self, output: str | Path = "docs/generated", spec_path: str | None = None
    ) -> list[str]:
        spec = load_spec(self.root / spec_path) if spec_path else None
        return [str(path) for path in write_docs(self.index, self.root / output, spec)]

    def manifests(
        self,
        name: str,
        image: str,
        port: int = 8000,
        output: str | Path = ".dev-agent/drafts",
    ) -> list[str]:
        return [
            str(path) for path in write_manifests(self.index, self.root / output, name, image, port)
        ]

    def write_project_memory(self) -> list[str]:
        memory = self.refresh()
        agents_md = write_agents_md(self.root, memory)
        return [agents_md.name]

    def create_task(self, goal: str) -> str:
        return self.store.create(goal).task_id

    def discovery(self, goal: str) -> dict:
        """Run the read-only discovery phase and journal it."""

        task_id = self.create_task(goal)
        analyst = AnalystRole().run(self.root)
        review = ReviewRole().run(self.root)
        security = SecurityRole().run(self.root)
        self.store.update(
            task_id,
            phase="review",
            checkpoint={
                "analysis": analyst.data.get("memory"),
                "review": review.data,
                "security": security.data,
            },
        )
        return {
            "task_id": task_id,
            "analysis": analyst.data.get("memory"),
            "review": review.data,
            "security": security.data,
        }
