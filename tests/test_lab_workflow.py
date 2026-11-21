"""The UI's lab tag may only claim what the workflow file proves.

The platform cannot read GitHub repository secrets, so the tag reports the
workflow's shape (physical runner labels, readiness gate, dispatch inputs,
secret names) and marks secret presence as not observable. These tests also
guard the checked-in workflow from silently losing its physical guarantees.
"""

import shutil
from pathlib import Path

import pytest
from core.lab_workflow import (
    KUBECONFIG_SECRETS,
    REQUIRED_ACCEPTANCE_LABELS,
    lab_workflow_report,
)
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/metal3-lab-e2e.yml"

MINIMAL_WORKFLOW = """
name: Lab
on:
  workflow_dispatch:
    inputs:
      project_id:
        required: true
        type: string
jobs:
  lab-readiness:
    runs-on: ubuntu-latest
    environment: metal3-lab
    outputs:
      ready: ${{ steps.readiness.outputs.ready }}
    steps:
      - run: echo "${{ secrets.EPH_KUBECONFIG }}"
  acceptance:
    needs: lab-readiness
    if: needs.lab-readiness.outputs.ready == 'true'
    runs-on: [self-hosted, metal3-lab, physical]
    steps:
      - run: echo "${{ secrets.TARGET_KUBECONFIG }}"
"""


def test_the_checked_in_workflow_keeps_its_physical_guarantees():
    report = lab_workflow_report(REPO_ROOT)

    assert report["available"] is True
    assert report["ready"] is True
    assert report["secret_presence"] == "not_observable"
    labels = report["acceptance_job"]["runs_on"]
    for label in REQUIRED_ACCEPTANCE_LABELS:
        assert label in labels
    assert report["acceptance_job"]["needs"] == "lab-readiness"
    for name in KUBECONFIG_SECRETS:
        assert name in report["secrets_referenced"]
    assert "project_id" in report["dispatch_inputs"]


def test_secrets_are_named_but_never_valued():
    report = lab_workflow_report(REPO_ROOT)

    assert report["secrets_referenced"] == ["EPH_KUBECONFIG", "TARGET_KUBECONFIG"]
    assert report["secret_presence"] == "not_observable"


def test_a_hosted_runner_is_never_reported_ready(tmp_path):
    workflow = tmp_path / ".github/workflows/metal3-lab-e2e.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        MINIMAL_WORKFLOW.replace(
            "runs-on: [self-hosted, metal3-lab, physical]",
            "runs-on: ubuntu-latest",
        ),
        encoding="utf-8",
    )

    report = lab_workflow_report(tmp_path)

    assert report["ready"] is False
    runner = next(
        check for check in report["checks"] if check["name"] == "acceptance.physical_runner"
    )
    assert runner["ok"] is False


def test_acceptance_without_the_readiness_gate_is_not_ready(tmp_path):
    workflow = tmp_path / ".github/workflows/metal3-lab-e2e.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        MINIMAL_WORKFLOW.replace("    needs: lab-readiness\n", "").replace(
            "    if: needs.lab-readiness.outputs.ready == 'true'\n",
            "",
        ),
        encoding="utf-8",
    )

    report = lab_workflow_report(tmp_path)

    assert report["ready"] is False
    failed = {check["name"] for check in report["checks"] if not check["ok"]}
    assert {"acceptance.readiness_gate", "acceptance.ready_condition"} <= failed


def test_missing_workflow_is_reported_not_guessed(tmp_path):
    report = lab_workflow_report(tmp_path)

    assert report["available"] is False
    assert report["ready"] is False
    assert report["reason"] == "LAB_WORKFLOW_NOT_PRESENT"
    assert report["secrets_referenced"] == []


def test_yaml_on_key_is_read_as_a_trigger_block(tmp_path):
    workflow = tmp_path / ".github/workflows/metal3-lab-e2e.yml"
    workflow.parent.mkdir(parents=True)
    shutil.copy(WORKFLOW, workflow)

    report = lab_workflow_report(tmp_path)

    # PyYAML resolves `on:` to boolean True; a naive lookup would report no inputs.
    assert report["dispatch_inputs"] == sorted(report["dispatch_inputs"])
    assert report["dispatch_inputs"]


@pytest.fixture
def api_user(db):
    user = get_user_model().objects.create_user(
        username="lab-viewer",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_endpoint_reports_the_same_shape(api_user, tmp_path):
    workflow = tmp_path / ".github/workflows/metal3-lab-e2e.yml"
    workflow.parent.mkdir(parents=True)
    shutil.copy(WORKFLOW, workflow)

    with override_settings(BASE_DIR=tmp_path):
        response = api_user.get("/api/v1/metal3/lab-workflow/")

    assert response.status_code == 200
    body = response.data
    assert body["available"] is True
    assert body["ready"] is True
    assert body["secret_presence"] == "not_observable"
    assert body["secrets_referenced"] == ["EPH_KUBECONFIG", "TARGET_KUBECONFIG"]
    assert body["workflow_url"] is None
