"""Static performance analysis.

The rules here are deliberately conservative: each finding points at code that
is almost always wasteful, without pretending to replace a profiler.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)
DB_CALLS = {"execute", "query", "fetch", "fetchone", "fetchall", "filter", "get", "find", "all"}
HTTP_CALLS = {"get", "post", "put", "patch", "delete", "request"}
DB_RECEIVERS = {"db", "session", "cursor", "conn", "connection", "repo", "repository", "collection"}
HTTP_RECEIVERS = {"requests", "httpx", "session", "client", "http"}


@dataclass
class PerformanceFinding:
    rule: str
    severity: str
    path: str
    line: int
    message: str
    suggestion: str

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _receiver(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id.lower()
    return ""


def _is_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _inside(nodes: list[ast.AST], position: int) -> bool:
    return any(
        node.lineno <= position <= getattr(node, "end_lineno", node.lineno) for node in nodes
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.loops: list[ast.AST] = []
        self.async_depth = 0
        self.findings: list[PerformanceFinding] = []

    def _add(self, rule: str, severity: str, line: int, message: str, suggestion: str) -> None:
        self.findings.append(
            PerformanceFinding(rule, severity, self.path, line, message, suggestion)
        )

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.async_depth += 1
        self.generic_visit(node)
        self.async_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._enter_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._enter_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._enter_loop(node)

    def _enter_loop(self, node: ast.AST) -> None:
        self.loops.append(node)
        if len(self.loops) >= 3:
            self._add(
                "nested_loops",
                "high",
                node.lineno,
                f"loop nest depth {len(self.loops)} implies cubic work",
                "Reduce the nest with a hash join, a precomputed index or a single pass.",
            )
        self.generic_visit(node)
        self.loops.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        receiver = _receiver(node)
        in_loop = bool(self.loops)
        if in_loop and name in DB_CALLS and receiver in DB_RECEIVERS:
            self._add(
                "n_plus_one",
                "high",
                node.lineno,
                f"{receiver}.{name}() runs once per iteration (N+1 query)",
                "Batch the query, fetch related rows with one join, or preload outside the loop.",
            )
        if in_loop and name in HTTP_CALLS and receiver in HTTP_RECEIVERS:
            self._add(
                "serial_http_in_loop",
                "medium",
                node.lineno,
                f"{receiver}.{name}() awaits one network round trip per iteration",
                "Use a single batch endpoint or bounded concurrency outside the loop.",
            )
        if in_loop and name in {"read", "read_text", "read_bytes"}:
            self._add(
                "whole_file_in_loop",
                "medium",
                node.lineno,
                f"{name}() reads a whole payload per iteration",
                "Read once outside the loop or stream fixed-size chunks.",
            )
        if self.async_depth and name == "sleep" and receiver in {"time", "asyncio"}:
            self._add(
                "blocking_sleep_in_async",
                "high",
                node.lineno,
                f"{receiver}.sleep() blocks the event loop",
                "Use asyncio.sleep() so other tasks can make progress.",
            )
        for argument in [*node.args, *[keyword.value for keyword in node.keywords]]:
            if _is_string(argument) and "select *" in argument.value.lower():
                self._add(
                    "select_star",
                    "medium",
                    node.lineno,
                    "SELECT * transfers columns the caller may not need",
                    "Select only the required columns and add LIMIT for list views.",
                )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self.loops and isinstance(node.op, ast.Add):
            if _is_string(node.value):
                self._add(
                    "quadratic_string_build",
                    "medium",
                    node.lineno,
                    "string concatenation inside a loop copies the whole prefix each pass",
                    "Collect fragments in a list and join once after the loop.",
                )
            elif isinstance(node.value, ast.List):
                self._add(
                    "quadratic_list_build",
                    "low",
                    node.lineno,
                    "list += inside a loop copies the accumulated list",
                    "Use list.extend() or accumulate in a local list.",
                )
        self.generic_visit(node)


def analyse_file(path: Path, root: Path) -> list[PerformanceFinding]:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return []
    visitor = _Visitor(relative)
    visitor.visit(tree)
    return visitor.findings


def analyse_performance(root: Path, paths: list[str] | None = None) -> list[PerformanceFinding]:
    root = root.resolve()
    if paths:
        candidates = [root / path for path in paths]
    else:
        candidates = sorted(
            path
            for path in root.rglob("*.py")
            if ".venv" not in path.parts and ".git" not in path.parts
        )
    findings: list[PerformanceFinding] = []
    for path in candidates:
        if path.is_file() and path.suffix == ".py":
            findings.extend(analyse_file(path, root))
    findings.sort(key=lambda item: (item.path, item.line, item.rule))
    return findings


def summarise(findings: list[PerformanceFinding]) -> dict:
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
    return {
        "total": len(findings),
        "by_severity": by_severity,
        "findings": [finding.as_dict() for finding in findings],
    }


def iter_python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if ".venv" not in path.parts and ".git" not in path.parts:
            yield path
