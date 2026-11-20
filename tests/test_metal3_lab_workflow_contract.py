"""The physical lab workflow must stay runnable and honestly gated.

The previous file put ``${{ runner.temp }}`` in a job-level ``env`` block, which
is not an allowed context. GitHub rejected the whole workflow, so it could
never execute. This contract test keeps the structural invariants that matter:
manual-only trigger, a readiness report that cannot fail the run for missing
secrets, and an acceptance job gated behind that report.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/metal3-lab-e2e.yml"


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _trigger(document: dict):
    # PyYAML follows YAML 1.1 and turns the ``on`` key into True.
    return document.get("on", document.get(True))


def _steps(job: dict) -> list[dict]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_the_workflow_only_runs_on_manual_dispatch():
    assert _trigger(_workflow()) == {
        "workflow_dispatch": {
            "inputs": {
                "project_id": {
                    "description": "Platform project id whose BMHs and machines are verified",
                    "required": True,
                    "type": "string",
                },
                "control_plane_replicas": {
                    "description": "Expected control-plane node count",
                    "required": True,
                    "default": "3",
                    "type": "string",
                },
                "worker_replicas": {
                    "description": "Expected worker node count",
                    "required": True,
                    "default": "4",
                    "type": "string",
                },
            }
        }
    }


def test_no_step_uses_a_context_that_is_invalid_for_its_scope():
    document = _workflow()
    for name, job in document["jobs"].items():
        env = job.get("env") or {}
        for key, value in env.items():
            assert "runner." not in str(value), (name, key)
            assert "steps." not in str(value), (name, key)


def test_readiness_reports_and_never_fakes_acceptance():
    job = _workflow()["jobs"]["lab-readiness"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["environment"] == "metal3-lab"
    assert job["outputs"]["ready"] == "${{ steps.readiness.outputs.ready }}"

    readiness = next(
        step for step in _steps(job) if step.get("id") == "readiness"
    )
    assert "tools/metal3_lab_readiness.py" in readiness["run"]
    assert "--github-output" in readiness["run"]

    uploads = [step for step in _steps(job) if "upload-artifact" in str(step.get("uses"))]
    assert uploads and all(step.get("if") == "always()" for step in uploads)


def test_acceptance_is_skipped_unless_the_report_is_ready():
    jobs = _workflow()["jobs"]
    acceptance = jobs["acceptance"]
    assert acceptance["needs"] == "lab-readiness"
    assert acceptance["if"] == "needs.lab-readiness.outputs.ready == 'true'"
    assert acceptance["runs-on"] == ["self-hosted", "metal3-lab", "physical"]
