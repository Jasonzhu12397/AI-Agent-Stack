"""Static understanding of a software project.

The scanner is deliberately dependency-free: it parses Python with :mod:`ast`
and inspects conventional project files. It never imports the target project,
so untrusted repositories cannot execute code while being analysed.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}


@dataclass
class FunctionInfo:
    name: str
    qualified_name: str
    line: int
    calls: list[str] = field(default_factory=list)
    docstring: str = ""
    node: ast.AST | None = None


@dataclass
class SymbolInfo:
    kind: str
    name: str
    line: int
    docstring: str = ""


@dataclass
class ModuleInfo:
    name: str
    path: str
    docstring: str = ""
    symbols: list[SymbolInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


@dataclass
class ProjectIndex:
    root: Path
    modules: dict[str, ModuleInfo]
    dependencies: dict[str, list[str]]
    technology: dict[str, Any]
    entrypoints: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "technology": self.technology,
            "entrypoints": self.entrypoints,
            "modules": {
                name: {
                    "path": module.path,
                    "docstring": module.docstring,
                    "symbols": [symbol.__dict__ for symbol in module.symbols],
                    "functions": [function.__dict__ for function in module.functions],
                    "imports": module.imports,
                }
                for name, module in sorted(self.modules.items())
            },
            "dependencies": {
                name: sorted(values) for name, values in sorted(self.dependencies.items())
            },
        }

    def outline(self, module_name: str | None = None) -> str:
        selected = (
            {module_name: self.modules[module_name]}
            if module_name and module_name in self.modules
            else self.modules
        )
        lines: list[str] = []
        for name, module in sorted(selected.items()):
            lines.append(f"{name} ({module.path})")
            if module.docstring:
                lines.append(f"  {module.docstring.splitlines()[0]}")
            for symbol in module.symbols:
                lines.append(f"  {symbol.kind} {symbol.name} @ {symbol.line}")
            for function in module.functions:
                lines.append(f"  def {function.name} @ {function.line}")
        return "\n".join(lines)


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem


def _resolve_imports(module_name: str, imports: list[str], known: set[str]) -> list[str]:
    resolved: list[str] = []
    package = module_name.rsplit(".", 1)[0] if "." in module_name else ""
    for imported in imports:
        candidates = [imported]
        if package:
            candidates.append(f"{package}.{imported}")
        for candidate in candidates:
            if candidate in known and candidate not in resolved:
                resolved.append(candidate)
                break
            prefix = candidate + "."
            if any(name.startswith(prefix) for name in known) and candidate not in resolved:
                resolved.append(candidate)
                break
    return resolved


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _analyse_module(root: Path, path: Path) -> ModuleInfo | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(source) > 2_000_000:
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return ModuleInfo(
            name=_module_name(root, path),
            path=str(path.relative_to(root)),
        )
    name = _module_name(root, path)
    module = ModuleInfo(
        name=name, path=str(path.relative_to(root)), docstring=ast.get_docstring(tree) or ""
    )
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = name.rsplit(".", 1)[0] if "." in name else ""
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base)
                target = f"{prefix}.{node.module}" if node.module else prefix
                imports.append(target)
            elif node.module:
                imports.append(node.module)
    module.imports = sorted(set(imports))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module.functions.append(_function_info(f"{name}.{node.name}", node))
            module.symbols.append(
                SymbolInfo("function", node.name, node.lineno, ast.get_docstring(node) or "")
            )
        elif isinstance(node, ast.ClassDef):
            module.symbols.append(
                SymbolInfo("class", node.name, node.lineno, ast.get_docstring(node) or "")
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    module.functions.append(
                        _function_info(f"{name}.{node.name}.{child.name}", child)
                    )
    return module


def _function_info(qualified_name: str, node: ast.AST) -> FunctionInfo:
    calls: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            called = _call_name(child.func)
            if called and called not in calls:
                calls.append(called)
    return FunctionInfo(
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        line=node.lineno,
        calls=calls,
        docstring=ast.get_docstring(node) or "",
        node=node,
    )


def _detect_technology(root: Path) -> dict[str, Any]:
    technology: dict[str, Any] = {"languages": [], "frameworks": [], "files": []}
    checks = {
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "setup.py": "python",
        "package.json": "javascript",
        "tsconfig.json": "typescript",
        "go.mod": "go",
        "Cargo.toml": "rust",
        "pom.xml": "java",
        "Dockerfile": "docker",
        "docker-compose.yml": "docker-compose",
    }
    for filename, language in checks.items():
        path = root / filename
        if path.exists():
            technology["files"].append(filename)
            if language not in technology["languages"]:
                technology["languages"].append(language)
    package = root / "package.json"
    if package.exists():
        try:
            document = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
        dependencies = {
            **(document.get("dependencies") or {}),
            **(document.get("devDependencies") or {}),
        }
        for framework in ("react", "vue", "next", "express", "jest", "vitest"):
            if framework in dependencies:
                technology["frameworks"].append(framework)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        for framework in ("django", "fastapi", "flask", "pytest", "ruff", "sqlalchemy"):
            if re.search(rf"\b{framework}\b", text, re.IGNORECASE):
                technology["frameworks"].append(framework)
    for pattern, name in (("manage.py", "django"), ("asgi.py", "asgi"), ("wsgi.py", "wsgi")):
        if any(root.rglob(pattern)) and name not in technology["frameworks"]:
            technology["frameworks"].append(name)
    technology["languages"] = sorted(set(technology["languages"]))
    technology["frameworks"] = sorted(set(technology["frameworks"]))
    technology["files"] = sorted(set(technology["files"]))
    return technology


def _entrypoints(root: Path) -> list[str]:
    candidates = [
        "manage.py",
        "main.py",
        "__main__.py",
        "cli.py",
        "app.py",
        "Dockerfile",
    ]
    found = [name for name in candidates if (root / name).exists()]
    for path in root.rglob("__main__.py"):
        if not any(part in SKIP_DIRECTORIES for part in path.parts):
            relative = str(path.relative_to(root))
            if relative not in found:
                found.append(relative)
    return sorted(found)


def scan_project(root: str | Path) -> ProjectIndex:
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"project root does not exist: {root}")
    modules: dict[str, ModuleInfo] = {}
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SKIP_DIRECTORIES]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            module = _analyse_module(root, path)
            if module is not None:
                modules[module.name] = module
    known = set(modules)
    dependencies: dict[str, list[str]] = {}
    for name, module in modules.items():
        dependencies[name] = _resolve_imports(name, module.imports, known)
    return ProjectIndex(
        root=root,
        modules=modules,
        dependencies=dependencies,
        technology=_detect_technology(root),
        entrypoints=_entrypoints(root),
    )


def module_outline(index: ProjectIndex, module_name: str) -> str:
    if module_name not in index.modules:
        raise KeyError(f"unknown module: {module_name}")
    module = index.modules[module_name]
    lines = [
        f"module {module.name} ({module.path})",
        f"imports: {', '.join(module.imports) or '-'}",
    ]
    if module.docstring:
        lines.append(f"doc: {module.docstring.splitlines()[0]}")
    for symbol in module.symbols:
        lines.append(f"{symbol.kind} {symbol.name} @{symbol.line}")
        if symbol.docstring:
            lines.append(f"  {symbol.docstring.splitlines()[0]}")
    return "\n".join(lines)


def call_chain(index: ProjectIndex, qualified_name: str, depth: int = 4) -> list[list[str]]:
    """Return call paths starting at ``qualified_name``.

    Static analysis cannot resolve dynamic dispatch, so this is an
    approximation that is still useful for orientation and review.
    """

    if not any(
        function.qualified_name == qualified_name
        for module in index.modules.values()
        for function in module.functions
    ):
        raise KeyError(f"unknown function: {qualified_name}")
    functions = {
        function.name: function
        for module in index.modules.values()
        for function in module.functions
    }
    paths: list[list[str]] = []

    def walk(name: str, current: list[str], remaining: int) -> None:
        if remaining <= 0:
            paths.append(current)
            return
        function = functions.get(name)
        if function is None or not function.calls:
            paths.append(current)
            return
        for called in function.calls:
            short = called.rsplit(".", 1)[-1]
            if short in current:
                paths.append(current)
            elif short in functions:
                walk(short, [*current, short], remaining - 1)
            else:
                paths.append([*current, called])

    walk(qualified_name.rsplit(".", 1)[-1], [qualified_name], depth)
    return paths
