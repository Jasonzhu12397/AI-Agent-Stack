"""Load vendored multi-document manifests for the EPH control stack."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from agent_contracts.kustomize_vars import KustomizeVarError, expand_kustomize_vars

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
DEFAULT_MANIFEST_DIR = Path("/opt/addons/control-stack")


class ControlStackManifestError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def manifest_directory(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("CONTROL_STACK_MANIFEST_DIR")
    return Path(configured) if configured else DEFAULT_MANIFEST_DIR


def load_component_manifests(
    component_names: list[str],
    directory: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return document lists keyed by component name.

    A component without a vendored file is omitted so the caller can still
    supply it explicitly; it is never invented here.
    """

    base = directory or manifest_directory()
    documents: dict[str, list[dict[str, Any]]] = {}
    for name in component_names:
        if not isinstance(name, str) or not name or "/" in name or ".." in name:
            raise ControlStackManifestError("CONTROL_STACK_MANIFEST_NAME_INVALID")
        path = base / f"{name}.yaml"
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ControlStackManifestError("CONTROL_STACK_MANIFEST_TOO_LARGE")
        try:
            text = expand_kustomize_vars(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ControlStackManifestError("CONTROL_STACK_MANIFEST_INVALID") from exc
        except KustomizeVarError as exc:
            raise ControlStackManifestError(exc.code) from exc
        try:
            loaded = list(yaml.safe_load_all(text))
        except (ValueError, yaml.YAMLError) as exc:
            raise ControlStackManifestError("CONTROL_STACK_MANIFEST_INVALID") from exc
        docs = [item for item in loaded if isinstance(item, dict) and item]
        if not docs:
            raise ControlStackManifestError("CONTROL_STACK_MANIFEST_EMPTY")
        documents[name] = docs
    return documents
