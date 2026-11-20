"""CLI entry point for the software engineering agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import SoftwareAgent
from .context import refresh_memory
from .openapi import load_spec, write_client
from .patching import TextEdit
from .testing import detect_test_plan


def _print(value) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-agent", description="Software engineering agent")
    parser.add_argument("--root", default=".", help="project root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="index modules, dependencies and entrypoints")
    scan.add_argument("--module", help="print only this module outline")
    scan.add_argument("--call-chain", help="describe the call chain for a qualified function")

    subparsers.add_parser("capabilities", help="report implemented and missing capabilities")

    review = subparsers.add_parser("review", help="static review findings")
    review.add_argument("paths", nargs="*")

    test = subparsers.add_parser("test", help="run the detected test suite")
    test.add_argument("--coverage", action="store_true")

    subparsers.add_parser("security", help="scan for credentials and dependency audit commands")
    subparsers.add_parser("memory", help="refresh project memory and rewrite AGENTS.md")
    subparsers.add_parser("git-status", help="git status and diff")
    subparsers.add_parser("git-commit-message", help="suggest a conventional commit message")
    subparsers.add_parser("test-plan", help="show the detected test command")

    debug = subparsers.add_parser("debug", help="analyse a traceback or gdb log")
    debug.add_argument("log", help="path to the log file, or - for stdin")

    docs = subparsers.add_parser("docs", help="generate README and architecture documents")
    docs.add_argument("--output", default="docs/generated")
    docs.add_argument("--openapi")

    manifests = subparsers.add_parser("manifests", help="generate Dockerfile, K8s and CI drafts")
    manifests.add_argument("--name", required=True)
    manifests.add_argument("--image", required=True)
    manifests.add_argument("--port", type=int, default=8000)

    openapi = subparsers.add_parser("openapi-client", help="generate an API client")
    openapi.add_argument("--spec", required=True)
    openapi.add_argument("--language", choices=["python", "ts"], required=True)
    openapi.add_argument("--output", required=True)

    patch = subparsers.add_parser(
        "plan-patch", help="validate edits and emit a diff plus approval token"
    )
    patch.add_argument("edits", help="JSON list of {path, old, new}")

    apply_patch = subparsers.add_parser("apply-patch", help="apply an approved edit plan")
    apply_patch.add_argument("edits")
    apply_patch.add_argument("--token", required=True)

    task = subparsers.add_parser("task", help="task journal")
    task.add_argument("action", choices=["create", "list", "show", "resume"])
    task.add_argument("value", nargs="?")

    discover = subparsers.add_parser("discover", help="run read-only discovery and journal it")
    discover.add_argument("goal")
    return parser


def _load_edits(path: str) -> list[TextEdit]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise SystemExit("edits file must contain a JSON list")
    return [TextEdit(**item) for item in document]


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.root)
    agent = SoftwareAgent(root)
    if arguments.command == "scan":
        if arguments.call_chain:
            _print(agent.analyse(arguments.call_chain)["call_chain"])
        elif arguments.module:
            _print(agent.analyse(arguments.module)["module"])
        else:
            _print(agent.analyse()["outline"])
    elif arguments.command == "capabilities":
        _print(agent.capabilities())
    elif arguments.command == "review":
        _print(agent.review(arguments.paths or None))
    elif arguments.command == "test":
        _print(agent.tests(arguments.coverage).as_dict())
    elif arguments.command == "test-plan":
        plan = detect_test_plan(root)
        _print({"command": plan.command, "framework": plan.framework} if plan else None)
    elif arguments.command == "security":
        _print(agent.security())
    elif arguments.command == "memory":
        refresh_memory(root)
        _print(
            {
                "memory": ".dev-agent/memory.json",
                "agents": "AGENTS.md" if agent.write_project_memory() else "",
            }
        )
    elif arguments.command == "git-status":
        _print(agent.git_status())
    elif arguments.command == "git-commit-message":
        _print(agent.commit_plan())
    elif arguments.command == "debug":
        log = (
            sys.stdin.read()
            if arguments.log == "-"
            else Path(arguments.log).read_text(encoding="utf-8")
        )
        _print(agent.debug(log))
    elif arguments.command == "docs":
        _print(agent.documents(arguments.output, arguments.openapi))
    elif arguments.command == "manifests":
        _print(agent.manifests(arguments.name, arguments.image, arguments.port))
    elif arguments.command == "openapi-client":
        destination = write_client(arguments.spec, arguments.language, arguments.output)
        load_spec(arguments.spec)
        _print(str(destination))
    elif arguments.command == "plan-patch":
        plan = agent.plan_change(_load_edits(arguments.edits))
        print(plan.diff)
        print(f"approval-token: {plan.token}")
    elif arguments.command == "apply-patch":
        _print(agent.apply_change(_load_edits(arguments.edits), arguments.token))
    elif arguments.command == "task":
        if arguments.action == "create":
            if not arguments.value:
                raise SystemExit("task create requires a goal")
            _print({"task_id": agent.create_task(arguments.value)})
        elif arguments.action == "list":
            _print([record.as_dict() for record in agent.store.list()])
        elif arguments.action == "show":
            _print(agent.store.get(arguments.value).as_dict())
        elif arguments.action == "resume":
            _print(agent.store.resume(arguments.value).as_dict())
    elif arguments.command == "discover":
        _print(agent.discovery(arguments.goal))
    return 0
