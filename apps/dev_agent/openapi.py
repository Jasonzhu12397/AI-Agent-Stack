"""OpenAPI ingestion and client generation for TypeScript and Python."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class Operation:
    method: str
    path: str
    operation_id: str
    parameters: list[dict]
    request_body: dict | None
    responses: dict


def load_spec(path: str | Path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("PyYAML is required to read YAML specifications") from exc
        document = yaml.safe_load(text)
    else:
        document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("OpenAPI document must be an object")
    return document


def _resolve(document: dict, value: Any, seen: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict) and "$ref" in value:
        reference = value["$ref"]
        if reference in seen:
            return value
        if not reference.startswith("#/"):
            return value
        target: Any = document
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(document, target, (*seen, reference))
    if isinstance(value, dict):
        return {key: _resolve(document, child, seen) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve(document, child, seen) for child in value]
    return value


def operations(spec: dict) -> list[Operation]:
    result: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId") or _derive_id(method, path)
            result.append(
                Operation(
                    method=method.lower(),
                    path=path,
                    operation_id=_identifier(operation_id),
                    parameters=_resolve(spec, operation.get("parameters", [])),
                    request_body=_resolve(spec, operation.get("requestBody")),
                    responses=_resolve(spec, operation.get("responses", {})),
                )
            )
    return result


def _derive_id(method: str, path: str) -> str:
    return method + "_" + re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")


def _identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if cleaned[:1].isdigit():
        cleaned = "_" + cleaned
    return cleaned or "operation"


def _path_parameters(operation: Operation) -> list[str]:
    names = re.findall(r"{([^}]+)}", operation.path)
    return [_identifier(name) for name in names]


def generate_python_client(spec: dict, class_name: str = "ApiClient") -> str:
    lines = [
        '"""Generated API client. Edit with care; regenerate from the OpenAPI spec."""',
        "",
        "from __future__ import annotations",
        "",
        "import httpx",
        "",
        "",
        f"class {class_name}:",
        "    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):",
        "        self.base_url = base_url.rstrip('/')",
        "        self.headers = {'Authorization': f'Bearer {token}'} if token else {}",
        "        self.timeout = timeout",
        "",
    ]
    for operation in operations(spec):
        path_args = _path_parameters(operation)
        query_args = [
            _identifier(parameter.get("name", "param"))
            for parameter in operation.parameters
            if parameter.get("in") == "query"
        ]
        signature = [
            "self",
            *[f"{name}: str" for name in path_args],
            *[f"{name}: str | None = None" for name in query_args],
        ]
        lines.append(f"    def {operation.operation_id}({', '.join(signature)}) -> dict:")
        path_expression = operation.path
        for name in re.findall(r"{([^}]+)}", operation.path):
            path_expression = path_expression.replace(
                "{" + name + "}", "{" + _identifier(name) + "}"
            )
        lines.append(f'        path = f"{path_expression}"')
        lines.append("        params = {")
        for name in query_args:
            lines.append(f'            "{name}": {name},')
        lines.append("        }")
        lines.append("        response = httpx.request(")
        lines.append(f'            "{operation.method.upper()}",')
        lines.append("            self.base_url + path,")
        lines.append("            params={k: v for k, v in params.items() if v is not None},")
        if query_args:
            pass
        lines.append("            headers=self.headers,")
        lines.append("            timeout=self.timeout,")
        lines.append("        )")
        lines.append("        response.raise_for_status()")
        lines.append("        return response.json()")
        lines.append("")
    return "\n".join(lines) + "\n"


def generate_typescript_client(spec: dict, class_name: str = "ApiClient") -> str:
    lines = [
        "// Generated API client. Regenerate from the OpenAPI specification.",
        "export class " + class_name + " {",
        "  constructor(private baseUrl: string, private token?: string) {}",
        "",
        "  private async call(path: string, init: RequestInit = {}): Promise<unknown> {",
        "    const response = await fetch(`${this.baseUrl}${path}`, {",
        "      ...init,",
        "      headers: {",
        "        'Content-Type': 'application/json',",
        "        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),",
        "        ...(init.headers ?? {}),",
        "      },",
        "    });",
        "    if (!response.ok) throw new Error(",
        "      `HTTP ${response.status}: ${await response.text()}`);",
        "    return response.json();",
        "  }",
    ]
    for operation in operations(spec):
        parameters = [
            _identifier(parameter.get("name", "param"))
            + ("?" if parameter.get("in") == "query" else "")
            + ": string"
            for parameter in operation.parameters
        ]
        signature = ", ".join(parameters)
        path_expression = operation.path
        for name in re.findall(r"{([^}]+)}", operation.path):
            path_expression = path_expression.replace(
                "{" + name + "}", "${" + _identifier(name) + "}"
            )
        lines.append("")
        lines.append(
            f"  async {operation.operation_id}({signature}): Promise<unknown> {{"
        )
        lines.append(
            "    return this.call(`"
            f"{path_expression}`"
            f", {{ method: '{operation.method.upper()}' }});"
        )
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_client(spec_path: str | Path, language: str, output: str | Path) -> Path:
    spec = load_spec(spec_path)
    if language == "python":
        content = generate_python_client(spec)
    elif language == "ts":
        content = generate_typescript_client(spec)
    else:
        raise ValueError("language must be python or ts")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def mock_example(spec: dict, path: str | Path | None = None) -> dict:
    """Build a minimal example response from the first 2xx operation."""

    chosen = None
    for operation in operations(spec):
        if path and operation.path != path:
            continue
        chosen = operation
        break
    if chosen is None:
        raise ValueError("no operation matches the requested path")
    response = next(
        (value for code, value in chosen.responses.items() if str(code).startswith("2")),
        {},
    )
    schema = (response or {}).get("content", {}).get("application/json", {}).get("schema", {})
    return {"operation": chosen.operation_id, "status": 200, "body": _example(schema)}


def _example(schema: Any, depth: int = 0) -> Any:
    if depth > 3 or not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _example(value, depth + 1)
            for key, value in (schema.get("properties") or {}).items()
            if key in (schema.get("required") or []) or depth == 0
        }
    if kind == "array":
        return [_example(schema.get("items", {}), depth + 1)]
    return {"string": "", "integer": 0, "number": 0.0, "boolean": False}.get(kind)
