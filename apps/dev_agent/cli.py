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
from .policy import ApprovalRequired
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

    work = subparsers.add_parser("work", help="execute or serve Cloud Control work items")
    work.add_argument("action", choices=["execute", "serve"])
    work.add_argument("--work-id", help="work id for execute")
    work.add_argument("--payload", help="JSON file or - for stdin (execute)")
    work.add_argument("--role", choices=["eph", "target"], help="agent role for serve")
    work.add_argument("--max-items", type=int, default=1)

    profile = subparsers.add_parser("profile", help="profile the test suite with cProfile")
    profile.add_argument("--top", type=int, default=15)

    collaborate = subparsers.add_parser(
        "collaborate", help="run analyst, review, security, performance and tests concurrently"
    )
    collaborate.add_argument("paths", nargs="*")

    plan = subparsers.add_parser("plan", help="ask an OpenAI-compatible model for a tool plan")
    plan.add_argument("--goal", required=True)
    plan.add_argument("--context")

    performance = subparsers.add_parser("performance", help="static performance findings")
    performance.add_argument("paths", nargs="*")
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

    comments = subparsers.add_parser(
        "plan-comments", help="propose docstrings for undocumented functions"
    )
    comments.add_argument("--limit", type=int, default=20)

    approve = subparsers.add_parser("approve", help="approve a pending plan token")
    approve.add_argument("token")
    approve.add_argument("--by", default="operator")

    subparsers.add_parser("approvals", help="list approval tokens and statuses")

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
    elif arguments.command == "performance":
        _print(agent.performance(arguments.paths or None))
    elif arguments.command == "work":
        from .work import SoftwareWorkExecutor, serve

        if arguments.action == "execute":
            if not arguments.work_id or not arguments.payload:
                raise SystemExit("work execute requires --work-id and --payload")
            document = json.loads(
                sys.stdin.read()
                if arguments.payload == "-"
                else Path(arguments.payload).read_text(encoding="utf-8")
            )
            work = (
                document
                if isinstance(document, dict) and "payload" in document
                else {"work_id": arguments.work_id, "kind": "software", "payload": document}
            )
            _print(SoftwareWorkExecutor(root).execute_work_item(work).as_dict())
        else:
            if not arguments.role:
                raise SystemExit("work serve requires --role")
            from packages.agent_transport.client import AgentControlClient
            from packages.agent_transport.config import AgentSettings

            client = AgentControlClient(AgentSettings.from_environment(arguments.role))
            try:
                _print(serve(client, root, max_items=arguments.max_items))
            finally:
                client.close()
    elif arguments.command == "profile":
        _print(agent.profile(arguments.top).as_dict())
    elif arguments.command == "collaborate":
        _print(agent.collaborate(arguments.paths or None))
    elif arguments.command == "plan":
        _print(agent.plan(arguments.goal, arguments.context))
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
        print("status: pending; run: dev-agent approve " + plan.token)
    elif arguments.command == "plan-comments":
        plan = agent.comment_plan(arguments.limit)
        print(plan.diff)
        print(f"approval-token: {plan.token}")
        print("status: pending; run: dev-agent approve " + plan.token)
    elif arguments.command == "approve":
        _print(agent.approve(arguments.token, arguments.by))
    elif arguments.command == "approvals":
        _print(agent.approvals.entries())
    elif arguments.command == "apply-patch":
        try:
            _print(agent.apply_change(_load_edits(arguments.edits), arguments.token))
        except ApprovalRequired as error:
            raise SystemExit(f"approval rejected: {error}") from error
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
