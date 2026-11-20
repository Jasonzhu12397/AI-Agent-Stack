"""Test discovery, execution, coverage parsing and a bounded repair loop."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import CommandResult, run


@dataclass
class TestPlan:
    program: str
    arguments: list[str]
    framework: str

    @property
    def command(self) -> list[str]:
        return [self.program, *self.arguments]


@dataclass
class TestReport:
    result: CommandResult
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    coverage: dict | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.returncode == 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.result.returncode,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "coverage": self.coverage,
            "failures": self.failures,
        }


def detect_test_plan(root: Path) -> TestPlan | None:
    root = root.resolve()
    if (root / "go.mod").exists():
        return TestPlan("go", ["test", "./..."], "go")
    package = root / "package.json"
    if package.exists():
        try:
            document = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
        if isinstance(document.get("scripts"), dict) and "test" in document["scripts"]:
            return TestPlan("npm", ["test", "--", "--run"], "npm")
    if (root / "pytest.ini").exists() or (root / "tests").is_dir():
        return TestPlan("python", ["-m", "pytest", "-q"], "pytest")
    return None


PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")
FAILURE_LINE = re.compile(r"(FAILED .+|ERROR .+)")


def parse_pytest_output(text: str) -> dict:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for amount, label in PYTEST_SUMMARY.findall(text):
        key = "errors" if label.startswith("error") else label
        counts[key] += int(amount)
    failures = []
    for line in text.splitlines():
        match = FAILURE_LINE.search(line.strip())
        if match and match.group(1) not in failures:
            failures.append(match.group(1))
    return {**counts, "failures": failures}


def load_coverage(root: Path) -> dict | None:
    path = root / ".dev-agent" / "coverage.json"
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    totals = document.get("totals")
    return totals if isinstance(totals, dict) else None


def run_tests(
    root: Path,
    plan: TestPlan | None = None,
    timeout: int = 300,
    coverage: bool = False,
) -> TestReport:
    plan = plan or detect_test_plan(root)
    if plan is None:
        raise RuntimeError("no supported test runner was detected")
    command = plan.command
    if plan.framework == "pytest":
        command = [sys.executable, "-m", "pytest", "-q"]
    if coverage and plan.framework == "pytest":
        command = [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q"]
    result = run(command, root, timeout=timeout)
    combined = f"{result.stdout}\n{result.stderr}"
    parsed = (
        parse_pytest_output(combined)
        if plan.framework == "pytest"
        else {
            "passed": 0,
            "failed": 0 if result.ok else 1,
            "errors": 0,
            "skipped": 0,
            "failures": []
            if result.ok
            else [combined.strip().splitlines()[-1] if combined.strip() else "test run failed"],
        }
    )
    report = TestReport(
        result=result,
        passed=parsed["passed"],
        failed=parsed["failed"],
        errors=parsed["errors"],
        skipped=parsed["skipped"],
        failures=parsed["failures"],
    )
    if coverage and plan.framework == "pytest":
        coverage_result = run([sys.executable, "-m", "coverage", "json"], root, timeout=60)
        if coverage_result.ok:
            report.coverage = load_coverage(root)
    return report


@dataclass
class RepairOutcome:
    status: str
    attempts: int
    report: TestReport | None
    history: list[str] = field(default_factory=list)


def repair_loop(
    root: Path,
    repair: Callable[[TestReport], bool],
    plan: TestPlan | None = None,
    max_attempts: int = 3,
    timeout: int = 300,
) -> RepairOutcome:
    """Run tests, apply a repair callback, and re-run until green or exhausted.

    The callback must return ``True`` only when it changed the workspace. A
    no-op repair stops the loop instead of burning attempts.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    history: list[str] = []
    report: TestReport | None = None
    for attempt in range(1, max_attempts + 1):
        report = run_tests(root, plan=plan, timeout=timeout)
        history.append(f"attempt {attempt}: {report.failed} failed, {report.errors} errors")
        if report.ok:
            return RepairOutcome("succeeded", attempt, report, history)
        repaired = repair(report)
        if not repaired:
            return RepairOutcome("needs-human", attempt, report, history)
    return RepairOutcome("exhausted", max_attempts, report, history)
