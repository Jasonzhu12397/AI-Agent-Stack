"""Role-specialised agents coordinated by the engineering orchestrator.

Each role only consumes the shared workspace and returns structured results;
none of them talks to a model or executes high-risk operations on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import build_memory
from .debugging import analyse_traceback, summarise
from .deployment import PreflightResult, preflight
from .reviewer import review_paths
from .scanner import call_chain, module_outline, scan_project
from .security import audit_commands, scan_secrets
from .testing import TestPlan, TestReport, detect_test_plan, repair_loop, run_tests


@dataclass
class RoleResult:
    role: str
    status: str = "succeeded"
    summary: str = ""
    data: dict = field(default_factory=dict)


class AnalystRole:
    name = "analyst"

    def run(self, root: Path, target: str | None = None) -> RoleResult:
        index = scan_project(root)
        memory = build_memory(index)
        data: dict[str, Any] = {"memory": memory.as_dict(), "outline": index.outline()}
        if target:
            if target in index.modules:
                data["module"] = module_outline(index, target)
            else:
                data["call_chain"] = call_chain(index, target)
        summary = (
            f"{len(index.modules)} modules, "
            f"{sum(len(module.functions) for module in index.modules.values())} functions"
        )
        return RoleResult(self.name, "succeeded", summary, data)


class ReviewRole:
    name = "reviewer"

    def run(self, root: Path, paths: list[str] | None = None) -> RoleResult:
        report = review_paths(root, paths)
        return RoleResult(
            self.name,
            "succeeded",
            f"{report.as_dict()['count']} findings, highest {report.highest_severity}",
            report.as_dict(),
        )


class TestRole:
    name = "tester"

    def __init__(
        self, repair: Callable[[TestReport], bool] | None = None, max_attempts: int = 3
    ) -> None:
        self.repair = repair
        self.max_attempts = max_attempts

    def plan(self, root: Path) -> TestPlan | None:
        return detect_test_plan(root)

    def run(self, root: Path, coverage: bool = False) -> RoleResult:
        report = run_tests(root, coverage=coverage)
        status = "succeeded" if report.ok else "failed"
        return RoleResult(
            self.name, status, f"{report.passed} passed, {report.failed} failed", report.as_dict()
        )

    def repair(self, root: Path) -> RoleResult:
        if self.repair is None:
            raise RuntimeError("a repair callback is required for the repair loop")
        outcome = repair_loop(root, self.repair, max_attempts=self.max_attempts)
        return RoleResult(
            self.name,
            "succeeded" if outcome.status == "succeeded" else "failed",
            f"{outcome.status} after {outcome.attempts} attempt(s)",
            {
                "history": outcome.history,
                "report": outcome.report.as_dict() if outcome.report else None,
            },
        )


class SecurityRole:
    name = "security"

    def run(self, root: Path) -> RoleResult:
        secrets = [finding.as_dict() for finding in scan_secrets(root)]
        commands = audit_commands(root)
        return RoleResult(
            self.name,
            "succeeded",
            f"{len(secrets)} credential-like strings, {len(commands)} audit command(s)",
            {"secrets": secrets, "audit_commands": commands},
        )


class DebugRole:
    name = "debugger"

    def run(self, log: str) -> RoleResult:
        report = analyse_traceback(log)
        return RoleResult(
            self.name,
            "succeeded" if report.exception_type else "failed",
            summarise(report),
            report.as_dict(),
        )


class DeployRole:
    name = "deployer"

    def run(
        self, nodes: list[dict], cpu_millicores: int = 100, memory_mib: int = 256
    ) -> RoleResult:
        result: PreflightResult = preflight(nodes, cpu_millicores, memory_mib)
        return RoleResult(
            self.name,
            "succeeded" if result.ready else "blocked",
            "cluster is ready for rollout" if result.ready else "; ".join(result.reasons),
            {"ready": result.ready, "reasons": result.reasons, "capacity": result.capacity},
        )


def analyse(root: Path, target: str | None = None) -> RoleResult:
    return AnalystRole().run(root, target)
