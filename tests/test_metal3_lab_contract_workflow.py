"""The lab contract workflow must keep proving the gate without bare metal.

The physical acceptance can only run on the lab runner. This workflow keeps the
YAML valid and the readiness semantics honest on every change, so a broken
workflow or a gate that turns green without real evidence is caught early.
"""

from pathlib import Path

import yaml

from tools import metal3_acceptance

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/metal3-lab-contract.yml"
LAB_WORKFLOW = REPO_ROOT / ".github/workflows/metal3-lab-e2e.yml"

WATCHED_PATHS = [
    ".github/workflows/metal3-lab-e2e.yml",
    ".github/workflows/metal3-lab-contract.yml",
    "tools/metal3_lab_readiness.py",
    "tools/metal3_acceptance.py",
    "tests/test_metal3_lab_workflow_contract.py",
    "tests/test_metal3_lab_contract_workflow.py",
    "pyproject.toml",
]


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _trigger(document: dict):
    return document.get("on", document.get(True))


def _steps() -> list[dict]:
    job = _workflow()["jobs"]["contract"]
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_contract_job_runs_on_ubuntu_and_only_watches_lab_files():
    document = _workflow()
    trigger = _trigger(document)
    assert "workflow_dispatch" in trigger
    assert trigger["push"]["paths"] == WATCHED_PATHS
    assert trigger["pull_request"]["paths"] == WATCHED_PATHS
    job = document["jobs"]["contract"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 15


def test_contract_job_runs_the_workflow_contract_tests():
    run = next(step for step in _steps() if step.get("name") == "Workflow contract tests")
    assert "tests/test_metal3_lab_workflow_contract.py" in run["run"]
    assert "tests/test_metal3_lab_contract_workflow.py" in run["run"]


def test_contract_job_checks_both_readiness_outcomes():
    run = next(
        step for step in _steps() if step.get("name") == "Readiness gate semantics"
    )
    # Missing secrets must never report ready, and a well-formed kubeconfig must.
    assert 'assert empty["ready"] is False' in run["run"]
    assert 'assert ready["ready"] is True' in run["run"]
    assert 'assert ready["failed_checks"] == []' in run["run"]


def test_contract_job_uploads_evidence_even_on_failure():
    uploads = [step for step in _steps() if "upload-artifact" in str(step.get("uses"))]
    assert uploads and all(step.get("if") == "always()" for step in uploads)


def test_physical_acceptance_still_requires_the_real_runner():
    document = yaml.safe_load(LAB_WORKFLOW.read_text(encoding="utf-8"))
    acceptance = document["jobs"]["acceptance"]
    assert acceptance["runs-on"] == ["self-hosted", "metal3-lab", "physical"]
    assert acceptance["if"] == "needs.lab-readiness.outputs.ready == 'true'"


def test_lab_workflow_offers_a_read_only_rollback_check():
    """The only rollback proof that can run on the lab is the read-only one."""

    document = yaml.safe_load(LAB_WORKFLOW.read_text(encoding="utf-8"))
    trigger = document.get("on", document.get(True))["workflow_dispatch"]["inputs"]
    assert "rollback_snapshot" in trigger
    assert trigger["rollback_snapshot"]["default"] == ""

    steps = document["jobs"]["acceptance"]["steps"]
    rollback = [step for step in steps if step.get("name") == "Rollback readiness (read-only)"]
    assert len(rollback) == 1
    assert rollback[0]["if"] == "inputs.rollback_snapshot != ''"
    run = rollback[0]["run"]
    assert "metal3_acceptance.py rollback" in run
    assert "--snapshot" in run
    assert "systemctl" not in run
    assert "sudo" not in run


def test_the_acceptance_tool_exposes_the_rollback_probe():
    parser = metal3_acceptance.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if type(action).__name__ == "_SubParsersAction"
    )
    assert "rollback" in subparsers.choices
    rollback = subparsers.choices["rollback"]
    assert rollback.get_default("func") is metal3_acceptance.command_rollback
    assert "--snapshot" in rollback._option_string_actions
