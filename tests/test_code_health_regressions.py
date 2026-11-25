from types import SimpleNamespace

from apps.dev_agent.roles import TestRole as DeveloperTestRole
from apps.dev_agent.testing import RepairOutcome
from apps.eph_agent import main as eph_main


def test_eph_readonly_runner_forwards_stdin(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(eph_main.subprocess, "run", fake_run)

    result = eph_main._run_read_only(
        ["kubectl", "apply", "--dry-run=client", "-f", "-"],
        timeout=30,
        stdin="apiVersion: v1\nkind: ConfigMap\n",
    )

    assert result["available"] is True
    assert result["stdout"] == "ok"
    assert captured["input"] == "apiVersion: v1\nkind: ConfigMap\n"
    assert captured["timeout"] == 30


def test_test_role_repair_method_is_callable_and_uses_the_callback(monkeypatch, tmp_path):
    callback = lambda report: True  # noqa: E731
    calls = {}

    def fake_repair_loop(root, repair, max_attempts=3):
        calls.update({"root": root, "repair": repair, "max_attempts": max_attempts})
        return RepairOutcome("succeeded", 1, None, ["attempt 1: green"])

    monkeypatch.setattr("apps.dev_agent.roles.repair_loop", fake_repair_loop)
    role = DeveloperTestRole(repair=callback, max_attempts=5)

    assert callable(role.repair)
    result = role.repair(tmp_path)

    assert result.status == "succeeded"
    assert calls == {"root": tmp_path, "repair": callback, "max_attempts": 5}
