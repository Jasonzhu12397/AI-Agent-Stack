"""Static code review with actionable findings and suggested diffs."""

from __future__ import annotations

import ast
import difflib
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import SKIP_DIRECTORIES
from .security import SECRET_PATTERNS

SQL_CALLS = {"execute", "executemany", "raw", "extra"}
NETWORK_CALLS = {"get", "post", "put", "patch", "delete", "request", "urlopen"}
NETWORK_MODULES = {"requests", "urllib3", "urllib.request"}
MUTABLE_DEFAULTS = (ast.List, ast.Dict, ast.Set)


@dataclass
class ReviewFinding:
    rule: str
    severity: str
    path: str
    line: int
    message: str
    suggestion: str = ""
    diff: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "suggestion": self.suggestion,
            "diff": self.diff,
        }


@dataclass
class ReviewReport:
    findings: list[ReviewFinding] = field(default_factory=list)

    @property
    def highest_severity(self) -> str:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        if not self.findings:
            return "none"
        return min((finding.severity for finding in self.findings), key=lambda value: order[value])

    def as_dict(self) -> dict:
        return {
            "highest_severity": self.highest_severity,
            "count": len(self.findings),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _line_diff(path: str, source: str, line: int, replacement: str) -> str:
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        return ""
    after = list(lines)
    after[line - 1] = replacement + ("\n" if lines[line - 1].endswith("\n") else "")
    return "".join(difflib.unified_diff(lines, after, fromfile=f"a/{path}", tofile=f"b/{path}"))


def _is_mutable(node: ast.AST | None) -> bool:
    return isinstance(node, MUTABLE_DEFAULTS)


def _is_test_path(path: str) -> bool:
    candidate = Path(path)
    return "tests" in candidate.parts or candidate.name.startswith("test_") or candidate.name.endswith(
        "_test.py"
    )


def _network_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Resolve explicit imports for network clients without guessing on method names."""

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".", 1)[0]
                if root in {"requests", "urllib3"}:
                    aliases[item.asname or root] = root
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in NETWORK_MODULES:
                for item in node.names:
                    aliases[item.asname or item.name] = f"{module}.{item.name}"
    return aliases


def _resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _resolved_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _resolved_name(node.func, aliases)
    return ""


def _network_without_timeout(node: ast.Call, aliases: dict[str, str]) -> bool:
    """Only flag known network-library calls, not ORM or dictionary methods."""

    if "timeout" in {keyword.arg for keyword in node.keywords}:
        return False
    name = _resolved_name(node.func, aliases)
    if name.rsplit(".", 1)[-1] not in NETWORK_CALLS:
        return False
    return any(name.startswith(f"{module}.") for module in NETWORK_MODULES)


def _sql_sink(node: ast.Call) -> str | None:
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if name not in SQL_CALLS or not node.args:
        return None
    argument = node.args[0]
    if isinstance(argument, ast.JoinedStr):
        return "f-string"
    if isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Mod):
        return "% formatting"
    if isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add):
        return "string concatenation"
    return None


def _review_tree(path: str, source: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [
            ReviewFinding(
                rule="syntax-error",
                severity="critical",
                path=path,
                line=exc.lineno or 0,
                message=f"{exc.msg}",
                suggestion="Fix the syntax error before running tests.",
            )
        ]

    lines = source.splitlines()
    network_aliases = _network_import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                original = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                findings.append(
                    ReviewFinding(
                        rule="bare-except",
                        severity="high",
                        path=path,
                        line=node.lineno,
                        message="Bare except catches KeyboardInterrupt and SystemExit.",
                        suggestion="Catch a specific exception or at least Exception.",
                        diff=_line_diff(
                            path,
                            source,
                            node.lineno,
                            original.replace("except:", "except Exception:", 1),
                        ),
                    )
                )
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                findings.append(
                    ReviewFinding(
                        rule="broad-except",
                        severity="low",
                        path=path,
                        line=node.lineno,
                        message="Broad Exception handler hides programming errors.",
                        suggestion="Catch the exceptions this call actually raises.",
                    )
                )
        elif isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name in {"eval", "exec"}:
                findings.append(
                    ReviewFinding(
                        rule="dynamic-code-execution",
                        severity="critical",
                        path=path,
                        line=node.lineno,
                        message=f"{name}() executes arbitrary code.",
                        suggestion="Parse input explicitly instead of executing it.",
                    )
                )
            if name == "run" or name == "Popen":
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value
                    ):
                        findings.append(
                            ReviewFinding(
                                rule="shell-injection",
                                severity="high",
                                path=path,
                                line=node.lineno,
                                message="subprocess runs through a shell.",
                                suggestion="Pass an argument list and keep shell=False.",
                            )
                        )
            if _network_without_timeout(node, network_aliases):
                findings.append(
                    ReviewFinding(
                        rule="missing-network-timeout",
                        severity="medium",
                        path=path,
                        line=node.lineno,
                        message=f"HTTP call {name}() has no timeout.",
                        suggestion="Set an explicit connect/read timeout.",
                    )
                )
            sink = _sql_sink(node)
            if sink:
                findings.append(
                    ReviewFinding(
                        rule="sql-injection",
                        severity="high",
                        path=path,
                        line=node.lineno,
                        message=f"SQL built with {sink} reaches execute().",
                        suggestion="Use parameterised queries with bound placeholders.",
                    )
                )
            if name == "assert" and False:  # pragma: no cover - assert is a statement
                pass
        elif isinstance(node, ast.Assert) and not _is_test_path(path):
            findings.append(
                ReviewFinding(
                    rule="assert-for-validation",
                    severity="low",
                    path=path,
                    line=node.lineno,
                    message="assert is removed under python -O.",
                    suggestion="Raise an explicit exception for runtime validation.",
                )
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.args, *node.args.kwonlyargs]
            for argument in arguments:
                default = None
                if argument in node.args.args:
                    index = node.args.args.index(argument)
                    offset = len(node.args.args) - len(node.args.defaults)
                    if index >= offset:
                        default = node.args.defaults[index - offset]
                elif argument in node.args.kwonlyargs:
                    index = node.args.kwonlyargs.index(argument)
                    value = node.args.kw_defaults[index]
                    if value is not None:
                        default = value
                if _is_mutable(default):
                    findings.append(
                        ReviewFinding(
                            rule="mutable-default-argument",
                            severity="high",
                            path=path,
                            line=node.lineno,
                            message=f"Function {node.name} uses a mutable default argument.",
                            suggestion="Default to None and create the container inside.",
                        )
                    )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                    value = node.value.value
                    if isinstance(value, str) and any(
                        pattern.search(value) for pattern in SECRET_PATTERNS
                    ):
                        findings.append(
                            ReviewFinding(
                                rule="hardcoded-secret",
                                severity="critical",
                                path=path,
                                line=node.lineno,
                                message=f"Possible credential assigned to {target.id}.",
                                suggestion=(
                                    "Load secrets from the environment or a vault reference."
                                ),
                            )
                        )
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and re.search(r"\b(TODO|FIXME|XXX)\b", token.string):
            findings.append(
                ReviewFinding(
                    rule="unfinished-marker",
                    severity="info",
                    path=path,
                    line=token.start[0],
                    message="Unfinished marker left in source comment.",
                    suggestion="Resolve it or link a tracked issue.",
                )
            )
    return findings


def review_paths(root: Path, paths: list[str] | None = None) -> ReviewReport:
    root = root.resolve()
    report = ReviewReport()
    if paths:
        candidates = [root / path for path in paths]
    else:
        candidates = [
            path
            for path in root.rglob("*.py")
            if not any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts)
        ]
    for path in sorted(candidates):
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        secondary = path.relative_to(root)
        report.findings.extend(_review_tree(str(secondary), source))
    return report
