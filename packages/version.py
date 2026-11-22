"""The one place the project version is read from.

The version is declared once, in ``pyproject.toml``. The API, the tests and any
release note that needs it read it from here instead of repeating a literal, so a
bump cannot leave the health endpoint or the acceptance evidence claiming the
previous version. ``AI_AGENT_STACK_VERSION`` overrides it for an image built
from a commit whose metadata was stripped.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_VERSION_ENV = "AI_AGENT_STACK_VERSION"
_PROJECT_FILE = Path(__file__).resolve().parents[1] / "pyproject.toml"
_PROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_FALLBACK = "0.0.0.dev0"


def _read_pyproject() -> str:
    try:
        text = _PROJECT_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _PROJECT_VERSION.search(text)
    return match.group(1).strip() if match else ""


@lru_cache(maxsize=1)
def project_version() -> str:
    """Return the project version, from the environment, pyproject or fallback."""

    declared = os.environ.get(_VERSION_ENV, "").strip()
    if declared:
        return declared
    return _read_pyproject() or _FALLBACK
