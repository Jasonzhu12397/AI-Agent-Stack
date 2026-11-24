"""Report authentication traffic recorded by the console API.

Deployment problems are hard to see from outside a codespace, so the API log is
summarised here: every login attempt with its status, plus CSRF rejections. That
tells apart "the browser never posted" from "the server rejected the post".
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG = REPO_ROOT / ".devdata" / "api.log"

PATTERNS = {
    "login": re.compile(r"POST /api/v1/auth/login/?\"? (\d{3})"),
    "csrf": re.compile(r"CSRF"),
    "me": re.compile(r"/api/v1/auth/me/\S*?\"? (\d{3})"),
}


def section(title: str, pattern: re.Pattern[str], lines: list[str], limit: int = 25) -> None:
    print(f"--- {title}", flush=True)
    hits = [line.strip() for line in lines if pattern.search(line)]
    for hit in hits[:limit]:
        print(hit[:200], flush=True)
    print(f"    total={len(hits)}", flush=True)


def main() -> None:
    if not LOG.exists():
        print(f"missing {LOG}", flush=True)
        return
    lines = LOG.read_text(errors="replace").splitlines()
    print(f"--- api.log lines={len(lines)}", flush=True)
    section("login attempts", PATTERNS["login"], lines)
    section("current-user probes", PATTERNS["me"], lines)
    section("csrf rejections", PATTERNS["csrf"], lines)


if __name__ == "__main__":
    main()
