"""The lab workflow report may only claim a real run when a token proves it."""

import json
from pathlib import Path

from core.lab_workflow import fetch_last_run, lab_workflow_report

WORKFLOW = """
name: metal3-lab-e2e
on:
  workflow_dispatch:
    inputs:
      cluster_name:
        type: string
jobs:
  lab-readiness:
    runs-on: [self-hosted, metal3-lab]
    outputs:
      ready: ${{ steps.probe.outputs.ready }}
    steps:
      - run: echo "${{ secrets.EPH_KUBECONFIG }}"
  acceptance:
    needs: lab-readiness
    if: needs.lab-readiness.outputs.ready == 'true'
    runs-on: [self-hosted, metal3-lab, physical]
    steps:
      - run: echo "${{ secrets.TARGET_KUBECONFIG }}"
"""


def _repo(tmp_path: Path) -> Path:
    path = tmp_path / ".github" / "workflows"
    path.mkdir(parents=True)
    (path / "metal3-lab-e2e.yml").write_text(WORKFLOW, encoding="utf-8")
    return tmp_path


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_last_run_is_reduced_to_the_fields_the_ui_renders(monkeypatch):
    payload = {
        "workflow_runs": [
            {
                "status": "completed",
                "conclusion": "success",
                "run_number": 42,
                "event": "workflow_dispatch",
                "created_at": "2026-11-20T10:00:00Z",
                "updated_at": "2026-11-20T10:40:00Z",
                "html_url": "https://github.com/o/r/actions/runs/42",
            }
        ]
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Response(payload))

    run = fetch_last_run("owner/repo", "token")

    assert run["conclusion"] == "success"
    assert run["run_number"] == 42
    assert set(run) == {
        "status",
        "conclusion",
        "run_number",
        "event",
        "created_at",
        "updated_at",
        "html_url",
    }


def test_a_failed_lookup_never_reports_a_run(monkeypatch):
    def explode(request, timeout):
        raise TimeoutError("no network")

    monkeypatch.setattr("urllib.request.urlopen", explode)

    assert fetch_last_run("owner/repo", "token") is None
    assert fetch_last_run("owner/repo", "") is None
    assert fetch_last_run("not-a-repository", "token") is None


def test_report_without_a_token_stays_not_observable(tmp_path):
    report = lab_workflow_report(_repo(tmp_path))

    assert report["last_run"] is None
    assert report["secret_presence"] == "not_observable"
    assert report["ready"] is True


def test_report_with_a_token_reports_the_run(tmp_path, monkeypatch):
    payload = {"workflow_runs": [{"conclusion": "failure", "run_number": 7}]}
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Response(payload))

    report = lab_workflow_report(_repo(tmp_path), repository="owner/repo", token="token")

    assert report["last_run"]["conclusion"] == "failure"
    assert report["secret_presence"] == "observed"


def test_workflow_file_that_does_not_exist_is_reported_as_absent(tmp_path):
    report = lab_workflow_report(tmp_path)

    assert report["available"] is False
    assert report["reason"] == "LAB_WORKFLOW_NOT_PRESENT"
