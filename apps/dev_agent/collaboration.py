"""Concurrent role collaboration.

Roles are independent readers of the same workspace. Running them concurrently
cuts wall-clock time without sharing mutable state; a failing role is reported as
failed data rather than aborting the whole run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .roles import RoleResult


@dataclass
class CollaborationReport:
    results: dict[str, RoleResult]
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return all(result.status != "failed" for result in self.results.values())

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "duration_seconds": round(self.duration_seconds, 3),
            "roles": {
                name: {**result.data, "status": result.status}
                for name, result in self.results.items()
            },
            "summaries": {name: result.summary for name, result in self.results.items()},
        }


def run_roles(
    tasks: dict[str, Callable[[], RoleResult]], max_workers: int = 4
) -> CollaborationReport:
    if not tasks:
        return CollaborationReport({}, 0.0)
    started = time.monotonic()
    results: dict[str, RoleResult] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        futures = {pool.submit(task): name for name, task in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as error:  # noqa: BLE001 - a role failure is data
                results[name] = RoleResult(name, "failed", f"{type(error).__name__}: {error}")
    elapsed = time.monotonic() - started
    ordered = {name: results[name] for name in tasks}
    return CollaborationReport(ordered, elapsed)


def default_tasks(
    root: Path, paths: list[str] | None = None
) -> dict[str, Callable[[], RoleResult]]:
    from .performance import analyse_performance, summarise
    from .roles import AnalystRole, ReviewRole, SecurityRole
    from .testing import run_tests

    def performance_role() -> RoleResult:
        findings = analyse_performance(root, paths)
        summary = summarise(findings)
        return RoleResult(
            "performance",
            "succeeded",
            f"{summary['total']} findings",
            summary,
        )

    def test_role() -> RoleResult:
        if not (root / "tests").exists() and not (root / "pytest.ini").exists():
            return RoleResult("tester", "skipped", "no test suite detected", {})
        report = run_tests(root)
        return RoleResult(
            "tester",
            "succeeded" if report.ok else "failed",
            f"{report.passed} passed, {report.failed} failed",
            report.as_dict(),
        )

    def analyst_role() -> RoleResult:
        return AnalystRole().run(root)

    return {
        "analyst": analyst_role,
        "reviewer": lambda: ReviewRole().run(root, paths),
        "security": lambda: SecurityRole().run(root),
        "performance": performance_role,
        "tester": test_role,
    }
