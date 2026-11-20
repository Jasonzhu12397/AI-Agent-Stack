#!/usr/bin/env python3
"""Post or update a static review comment on a pull request.

This script is intentionally dependency-free so a GitHub Actions runner can
execute it with only the repository checked out. It never executes pull request
code; it only parses changed Python files with `ast`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.dev_agent.performance import analyse_performance  # noqa: E402
from apps.dev_agent.reviewer import review_paths  # noqa: E402

MARKER = "<!-- dev-agent-review -->"
MAX_ITEMS = 30


def changed_python_files(base: str, head: str) -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line.endswith(".py")]


def render(files: list[str], root: Path) -> str:
    if not files:
        return "\n".join([MARKER, "## Dev agent review", "", "No changed Python files."])
    review = review_paths(root, files)
    performance = analyse_performance(root, files)
    lines = [MARKER, "## Dev agent review", ""]
    lines.append(f"Changed Python files: **{len(files)}**")
    lines.append(
        f"Review findings: **{len(review.findings)}** (highest: {review.highest_severity})"
    )
    lines.append(f"Performance findings: **{len(performance)}**")
    lines.append("")
    if review.findings:
        lines.extend(
            ["### Review", "", "| Severity | Location | Rule | Message |", "|---|---|---|---|"]
        )
        for finding in review.findings[:MAX_ITEMS]:
            lines.append(
                f"| {finding.severity} | `{finding.path}:{finding.line}` | "
                f"{finding.rule} | {finding.message} |"
            )
        if len(review.findings) > MAX_ITEMS:
            lines.append(f"| ... | | | {len(review.findings) - MAX_ITEMS} more |")
        lines.append("")
    if performance:
        lines.extend(
            ["### Performance", "", "| Severity | Location | Rule | Message |", "|---|---|---|---|"]
        )
        for finding in performance[:MAX_ITEMS]:
            lines.append(
                f"| {finding.severity} | `{finding.path}:{finding.line}` | "
                f"{finding.rule} | {finding.message} |"
            )
        lines.append("")
    if not review.findings and not performance:
        lines.append("No static findings on the changed files.")
        lines.append("")
    lines.append("Generated from static analysis only; no pull request code was executed.")
    return "\n".join(lines)


def request(method: str, url: str, payload: dict | None = None) -> dict | list:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30
    ) as response:
        return json.loads(response.read().decode())


def publish(body: str) -> str:
    repository = os.environ["GITHUB_REPOSITORY"]
    number = os.environ["PR_NUMBER"]
    comments = request(
        "GET", f"https://api.github.com/repos/{repository}/issues/{number}/comments?per_page=100"
    )
    for comment in comments:
        if isinstance(comment, dict) and str(comment.get("body", "")).startswith(MARKER):
            request(
                "PATCH",
                f"https://api.github.com/repos/{repository}/issues/comments/{comment['id']}",
                {"body": body},
            )
            return "updated"
    request(
        "POST",
        f"https://api.github.com/repos/{repository}/issues/{number}/comments",
        {"body": body},
    )
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--root", default=".")
    arguments = parser.parse_args()
    files = changed_python_files(arguments.base, arguments.head)
    body = render(files, Path(arguments.root).resolve())
    print(body)
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("PR_NUMBER"):
        print("GITHUB_TOKEN or PR_NUMBER missing; comment not published.", file=sys.stderr)
        return 0
    try:
        action = publish(body)
    except urllib.error.HTTPError as error:
        print(f"GitHub API rejected the comment: {error}", file=sys.stderr)
        return 0
    print(f"Comment {action}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
