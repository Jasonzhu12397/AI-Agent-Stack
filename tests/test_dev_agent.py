from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.dev_agent.agent import SoftwareAgent
from apps.dev_agent.checkpoint import TaskStore
from apps.dev_agent.collaboration import run_roles
from apps.dev_agent.context import build_memory, refresh_memory, write_agents_md
from apps.dev_agent.debugging import analyse_traceback, parse_gdb_backtrace
from apps.dev_agent.deployment import generate_ci, generate_dockerfile, preflight, rollback_steps
from apps.dev_agent.docs import docstring_edits, render_architecture, render_readme
from apps.dev_agent.gitops import (
    append_changelog,
    changelog_entry,
    commit_message,
    has_conflict_markers,
)
from apps.dev_agent.model import ModelOutputError, parse_tool_call, validate_schema
from apps.dev_agent.openapi import generate_python_client, generate_typescript_client, mock_example
from apps.dev_agent.patching import PatchError, TextEdit, plan
from apps.dev_agent.performance import analyse_performance, summarise
from apps.dev_agent.planner import OpenAICompatiblePlanner, PlannerConfig, PlannerError
from apps.dev_agent.policy import ApprovalRequired, PolicyError, read_text
from apps.dev_agent.reviewer import review_paths
from apps.dev_agent.roles import RoleResult
from apps.dev_agent.sandbox import SandboxViolation, run, validate_command
from apps.dev_agent.scanner import call_chain, module_outline, scan_project
from apps.dev_agent.security import parse_npm_audit, parse_pip_audit, scan_secrets
from apps.dev_agent.testing import detect_test_plan, parse_pytest_output, repair_loop


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\nversion='1'\n")
    (tmp_path / "pkg" / "__init__.py").write_text('"""Sample package."""\n')
    (tmp_path / "pkg" / "core.py").write_text(
        '"""Core logic."""\n\nimport os\n\n\ndef load(path):\n    """Load a file."""\n'
        "    return read(path)\n\n\ndef read(path):\n    with open(path) as handle:\n"
        "        return handle.read()\n"
    )
    (tmp_path / "pkg" / "cli.py").write_text(
        "from .core import load\n\n\ndef main():\n    return load('x')\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import read\n\n\ndef test_read(tmp_path):\n"
        "    path = tmp_path / 'a'\n    path.write_text('ok')\n"
        "    assert read(str(path)) == 'ok'\n"
    )
    return tmp_path


def test_scan_project_maps_modules_and_dependencies(project):
    index = scan_project(project)
    assert set(index.modules) == {"pkg", "pkg.core", "pkg.cli", "tests.test_core"}
    assert "pkg.core" in index.dependencies["pkg.cli"]
    assert index.technology["languages"] == ["python"]
    outline = index.outline()
    assert "pkg.core" in outline
    assert "def load" in outline


def test_module_outline_and_call_chain(project):
    index = scan_project(project)
    assert "Core logic." in module_outline(index, "pkg.core")
    paths = call_chain(index, "pkg.core.load")
    assert any(path[:2] == ["pkg.core.load", "read"] for path in paths)


def test_reviewer_reports_bare_except_sql_and_secrets(tmp_path):
    (tmp_path / "bad.py").write_text(
        "import sqlite3\n\n\ndef query(connection, name):\n"
        "    try:\n        pass\n    except:\n        pass\n"
        '    connection.execute(f"SELECT * FROM t WHERE n = {name}")\n'
        "    api_key = 'sk-" + "a" * 40 + "'\n"
        "    import subprocess\n    subprocess.run('ls', shell=True)\n"
    )
    report = review_paths(tmp_path)
    rules = {finding.rule for finding in report.findings}
    assert {"bare-except", "sql-injection", "hardcoded-secret", "shell-injection"} <= rules
    bare = next(finding for finding in report.findings if finding.rule == "bare-except")
    assert "except Exception:" in bare.diff


def test_patch_requires_matching_one_time_approval(project):
    agent = SoftwareAgent(project)
    edits = [TextEdit("pkg/core.py", "return read(path)", "return read(path).strip()")]
    planned = agent.plan_change(edits)
    assert "-    return read(path)" in planned.diff
    with pytest.raises(ApprovalRequired):
        agent.apply_change(edits, "not-a-token")
    assert agent.apply_change(edits, planned.token) == ["pkg/core.py"]
    assert "return read(path).strip()" in (project / "pkg" / "core.py").read_text()
    with pytest.raises(ApprovalRequired):
        agent.apply_change(edits, planned.token)


def test_patch_rejects_ambiguous_or_escaping_edits(project, tmp_path):
    with pytest.raises(PolicyError):
        read_text(project, "../outside.txt")
    assert plan([TextEdit("pkg/core.py", "return read(path)", "return x")], project)
    with pytest.raises(PatchError):
        plan([TextEdit("pkg/core.py", "nomatch", "x")], project)


def test_checkpoint_store_resumes_running_tasks(tmp_path):
    store = TaskStore(tmp_path)
    record = store.create("fix failing tests")
    updated = store.update(record.task_id, phase="test", checkpoint={"attempt": 1})
    assert updated.phase == "test"
    resumed = store.resume(record.task_id)
    assert resumed.status == "running"
    assert resumed.checkpoint["attempt"] == 1
    store.update(record.task_id, status="succeeded")
    with pytest.raises(ValueError):
        store.resume(record.task_id)


def test_test_discovery_and_output_parsing(project):
    plan = detect_test_plan(project)
    assert plan.framework == "pytest"
    parsed = parse_pytest_output(
        "2 passed, 1 failed, 1 skipped in 0.20s\nFAILED tests/test_x.py::test_y"
    )
    assert parsed == {
        "passed": 2,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
        "failures": ["FAILED tests/test_x.py::test_y"],
    }


def test_repair_loop_stops_when_repair_makes_no_change(project, monkeypatch):
    calls = {"repair": 0}

    class FakeReport:
        ok = False
        failed = 1
        errors = 0

        def as_dict(self):
            return {"ok": False}

    monkeypatch.setattr("apps.dev_agent.testing.run_tests", lambda *a, **k: FakeReport())
    outcome = repair_loop(project, lambda report: calls.__setitem__("repair", 1) or False)
    assert outcome.status == "needs-human"
    assert calls["repair"] == 1


def test_debugging_parses_tracebacks_and_gdb_frames():
    log = (
        'Traceback (most recent call last):\n  File "/app/main.py", line 10, in run\n'
        "    handler()\n  File \"/app/handler.py\", line 3, in handler\n    value['x']\n"
        "KeyError: 'x'\n"
    )
    report = analyse_traceback(log)
    assert report.exception_type == "KeyError"
    assert report.origin.file == "/app/handler.py"
    assert report.origin.line == 3
    frames = parse_gdb_backtrace(
        "#0  0x0001 in crash () at core.c:42\n#1  0x0002 in main () at main.c:7"
    )
    assert [frame.file for frame in frames] == ["core.c", "main.c"]


def test_git_helpers(project, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Agent"], cwd=project, check=True)
    message = commit_message(["docs/readme.md", "README.md"], summary="document usage")
    assert message.startswith("docs(repo): document usage")
    assert commit_message(["src/a.py"]).startswith("feat(src):")
    (tmp_path / "conflict.txt").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n")
    assert has_conflict_markers(tmp_path) == ["conflict.txt"]
    append_changelog(tmp_path, changelog_entry("1.1.0", ["fixed tests"]))
    assert "1.1.0" in (tmp_path / "CHANGELOG.md").read_text()


def test_security_scan_and_audit_parsers(tmp_path):
    (tmp_path / "settings.py").write_text("TOKEN = 'ghp_" + "b" * 40 + "'\n")
    findings = scan_secrets(tmp_path)
    assert findings and findings[0].path == "settings.py"
    pip = parse_pip_audit(
        {
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.0",
                    "vulns": [{"id": "CVE-1", "fix_versions": ["2.1"]}],
                }
            ]
        }
    )
    assert pip[0]["package"] == "requests"
    npm = parse_npm_audit(
        {"vulnerabilities": {"lodash": {"severity": "high", "range": "<4", "fixAvailable": True}}}
    )
    assert npm[0]["severity"] == "high"


def test_openapi_generates_clients_and_examples(tmp_path):
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/pets/{pet_id}": {
                "get": {
                    "operationId": "get_pet",
                    "parameters": [
                        {"name": "pet_id", "in": "path", "required": True},
                        {"name": "verbose", "in": "query"},
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    python = generate_python_client(spec)
    assert "def get_pet" in python and "httpx.request" in python
    typescript = generate_typescript_client(spec)
    assert "async get_pet" in typescript and "fetch" in typescript
    example = mock_example(spec)
    assert example["operation"] == "get_pet"
    assert example["body"] == {"name": ""}


def test_docs_and_deployment_artifacts(project):
    agent = SoftwareAgent(project)
    docs = agent.documents(output="docs/generated")
    assert len(docs) == 2
    assert "## Modules" in render_readme(agent.index)
    assert "graph TD" in render_architecture(agent.index)
    manifests = agent.manifests("sample", "registry/sample:1")
    assert {Path(path).name for path in manifests} == {
        "Dockerfile.generated",
        "deployment.yaml",
        "service.yaml",
        "ci.generated.yml",
    }
    assert "python -m pytest" in generate_ci({"languages": ["python"]})
    assert generate_dockerfile(agent.index).startswith("FROM python:3.12-slim")


def test_preflight_and_rollback_plan():
    assert preflight(
        [{"ready": True, "allocatable_cpu_millicores": 400, "allocatable_memory_mib": 1024}]
    ).ready
    blocked = preflight([{"ready": False}])
    assert not blocked.ready
    assert "no node reports Ready" in blocked.reasons[0]
    assert rollback_steps("sample")[-1].startswith("confirm readiness")


def test_sandbox_denies_high_risk_commands(project):
    assert run(["git", "init"], project).returncode == 0
    assert run(["git", "status"], project).returncode == 0
    with pytest.raises(SandboxViolation):
        validate_command(["git", "push", "origin", "main"])
    with pytest.raises(SandboxViolation):
        validate_command(["bash", "-c", "rm -rf /"])
    with pytest.raises(SandboxViolation):
        validate_command(["python", "-c", "import os"])
    result = run([sys.executable, "-m", "pytest", "-q"], project, timeout=120)
    assert result.returncode == 0


def test_model_output_schema_is_enforced():
    call = parse_tool_call('```json\n{"tool": "run_tests", "arguments": {"coverage": true}}\n```')
    assert call["arguments"]["coverage"] is True
    with pytest.raises(ModelOutputError):
        validate_schema(
            {"tool": "run_tests"}, {"type": "object", "required": ["tool", "arguments"]}
        )


def test_project_memory_and_agents_md(project):
    memory = refresh_memory(project)
    assert memory.conventions["line_length"] == 100
    write_agents_md(project, memory)
    assert "## Guardrails" in (project / "AGENTS.md").read_text()
    assert build_memory(scan_project(project)).technology["languages"] == ["python"]


def test_agent_discovery_is_journaled(project):
    agent = SoftwareAgent(project)
    result = agent.discovery("inspect the project")
    assert result["task_id"].startswith("task-")
    record = agent.store.get(result["task_id"])
    assert record.phase == "review"
    assert record.checkpoint["review"]["count"] >= 0
    assert "sandbox" in agent.capabilities()


def test_performance_rules_cover_n_plus_one_and_loop_nesting(tmp_path):
    (tmp_path / "slow.py").write_text(
        "import asyncio\n\nasync def poll(db):\n    await asyncio.sleep(1)\n\n"
        "def scan(db):\n    text = ''\n    for a in range(10):\n        for b in range(10):\n"
        "            for c in range(10):\n                db.execute('select * from t')\n"
        "                text += 'x'\n"
    )
    findings = analyse_performance(tmp_path)
    rules = {finding.rule for finding in findings}
    assert {"nested_loops", "n_plus_one", "select_star"} <= rules
    assert "blocking_sleep_in_async" in rules
    assert "quadratic_string_build" in rules
    summary = summarise(findings)
    assert summary["total"] == len(findings)
    assert summary["by_severity"]["high"] >= 2


def test_docstring_plan_is_approval_gated(project):
    index = scan_project(project)
    edits = docstring_edits(index)
    assert edits
    agent = SoftwareAgent(project)
    plan = agent.comment_plan()
    assert plan.token
    assert plan.diff
    applied = agent.apply_change(edits, plan.token)
    assert "pkg/cli.py" in " ".join(applied)
    source = (project / "pkg" / "cli.py").read_text()
    assert '"""Main."""' in source
    compile(source, "pkg/cli.py", "exec")


def test_planner_retries_and_validates_steps():
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            raise TimeoutError("transient")
        content = json.dumps(
            {
                "steps": [
                    {
                        "tool": "run_tests",
                        "arguments": {"coverage": True},
                        "rationale": "prove the baseline",
                    }
                ]
            }
        )
        return {"choices": [{"message": {"content": content}}]}

    planner = OpenAICompatiblePlanner(
        PlannerConfig(
            base_url="https://llm.example/v1",
            model="test-model",
            api_key="secret",
            max_retries=2,
            backoff_seconds=0,
        ),
        transport=transport,
    )
    plan = planner.plan("stabilise tests", "context")
    assert plan.steps[0]["tool"] == "run_tests"
    assert len(calls) == 2
    assert calls[0]["model"] == "test-model"


def test_planner_rejects_tools_outside_the_allowlist():
    def transport(url, headers, payload, timeout):
        content = json.dumps(
            {"steps": [{"tool": "deploy", "arguments": {}, "rationale": "ship it"}]}
        )
        return {"choices": [{"message": {"content": content}}]}

    planner = OpenAICompatiblePlanner(
        PlannerConfig(base_url="https://llm.example/v1", model="m"), transport=transport
    )
    with pytest.raises(ModelOutputError):
        planner.plan("do something")


def test_planner_requires_http_base_url():
    with pytest.raises(PlannerError):
        OpenAICompatiblePlanner(PlannerConfig(base_url="llm.example", model="m"))


def test_collaboration_runs_roles_concurrently_and_keeps_failures():
    import threading

    barrier = threading.Barrier(3)

    def role(name):
        def run():
            barrier.wait(timeout=2)
            return RoleResult(name, "succeeded", name, {"name": name})

        return run

    def broken():
        barrier.wait(timeout=2)
        raise ValueError("boom")

    report = run_roles(
        {"analyst": role("analyst"), "reviewer": role("reviewer"), "security": broken},
        max_workers=3,
    )
    assert report.results["analyst"].status == "succeeded"
    assert report.results["reviewer"].status == "succeeded"
    assert report.results["security"].status == "failed"
    assert report.ok is False
    assert report.as_dict()["roles"]["analyst"]["status"] == "succeeded"
