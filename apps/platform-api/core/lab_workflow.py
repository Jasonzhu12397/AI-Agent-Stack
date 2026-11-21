"""Static probe for the physical Metal3 lab acceptance workflow.

The platform cannot read GitHub repository secrets, so it must not pretend to
know whether the lab is ready. What it can judge is the workflow's *shape*: the
runner labels that decide whether a real lab machine is reached, the readiness
gate in front of the acceptance job, the declared dispatch inputs, and the
secrets the workflow will consume by name.

This module only parses the workflow file; it never calls GitHub. The UI shows
the result as a tag plus an explicit "secret presence is verified in the CI
readiness report, not here" note.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = ".github/workflows/metal3-lab-e2e.yml"
REQUIRED_ACCEPTANCE_LABELS = ("self-hosted", "metal3-lab", "physical")
ACCEPTANCE_JOB_NAMES = ("acceptance", "lab-acceptance")
KUBECONFIG_SECRETS = ("EPH_KUBECONFIG", "TARGET_KUBECONFIG")


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _labels(job: dict[str, Any]) -> list[str]:
    runs_on = job.get("runs-on")
    if isinstance(runs_on, list):
        return [str(item) for item in runs_on]
    if isinstance(runs_on, str):
        return [part.strip() for part in runs_on.split(",") if part.strip()]
    return []


def _secret_names(document: Any) -> list[str]:
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and "secrets." in value:
            for chunk in value.split("secrets.")[1:]:
                name = ""
                for char in chunk:
                    if char.isalnum() or char == "_":
                        name += char
                    else:
                        break
                if name:
                    names.add(name)

    walk(document)
    return sorted(names)


def inspect_lab_workflow(path: Path) -> dict[str, Any]:
    """Return the workflow's checks, or ``available: False`` when unreadable."""

    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "available": False,
        "workflow": WORKFLOW_PATH,
        "checks": checks,
        "dispatch_inputs": [],
        "secrets_referenced": [],
        "environment": None,
        "ready": False,
        "secret_presence": "not_observable",
        "reason": None,
    }
    if not path.is_file():
        checks.append(_check("workflow.present", False, f"{WORKFLOW_PATH} is not deployed here"))
        result["reason"] = "LAB_WORKFLOW_NOT_PRESENT"
        return result
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        checks.append(_check("workflow.parse", False, "workflow is not valid YAML"))
        result["reason"] = "LAB_WORKFLOW_INVALID"
        return result
    if not isinstance(document, dict):
        checks.append(_check("workflow.parse", False, "workflow is not a mapping"))
        result["reason"] = "LAB_WORKFLOW_INVALID"
        return result

    result["available"] = True
    checks.append(_check("workflow.present", True, WORKFLOW_PATH))

    # PyYAML resolves the unquoted `on:` key to boolean True (YAML 1.1), so the
    # trigger block has to be looked up under both spellings.
    triggers = document.get("on")
    if triggers is None:
        triggers = document.get(True)
    dispatch = (triggers or {}).get("workflow_dispatch")
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    input_names = sorted(inputs) if isinstance(inputs, dict) else []
    result["dispatch_inputs"] = input_names
    checks.append(
        _check(
            "workflow.dispatch_inputs",
            bool(input_names),
            ", ".join(input_names) or "no workflow_dispatch inputs declared",
        )
    )

    jobs = document.get("jobs")
    jobs = jobs if isinstance(jobs, dict) else {}
    readiness = jobs.get("lab-readiness") if isinstance(jobs, dict) else None
    acceptance = None
    for job_name in ACCEPTANCE_JOB_NAMES:
        acceptance = jobs.get(job_name)
        if isinstance(acceptance, dict):
            break
    result["readiness_job"] = {
        "name": (readiness or {}).get("name"),
        "runs_on": _labels(readiness or {}),
        "environment": (readiness or {}).get("environment"),
        "outputs": sorted(((readiness or {}).get("outputs") or {}).keys()),
    }
    acceptance_labels = _labels(acceptance or {})
    result["acceptance_job"] = {
        "name": (acceptance or {}).get("name"),
        "runs_on": acceptance_labels,
        "needs": (acceptance or {}).get("needs"),
        "environment": (acceptance or {}).get("environment"),
    }
    result["environment"] = (readiness or {}).get("environment")

    missing_labels = [
        label for label in REQUIRED_ACCEPTANCE_LABELS if label not in acceptance_labels
    ]
    checks.append(
        _check(
            "acceptance.physical_runner",
            bool(acceptance_labels) and not missing_labels,
            (
                "runs-on " + ", ".join(acceptance_labels)
                if acceptance_labels and not missing_labels
                else "missing label(s): " + (", ".join(missing_labels) or "runs-on not declared")
            ),
        )
    )

    needs = (acceptance or {}).get("needs")
    needs_list = needs if isinstance(needs, list) else ([needs] if isinstance(needs, str) else [])
    gate = "lab-readiness" in needs_list
    checks.append(
        _check(
            "acceptance.readiness_gate",
            gate,
            "needs lab-readiness" if gate else "acceptance is not gated on the readiness job",
        )
    )

    job_when = str((acceptance or {}).get("if") or "")
    gated_on_ready = "ready" in job_when and "true" in job_when
    checks.append(
        _check(
            "acceptance.ready_condition",
            gated_on_ready,
            job_when or "acceptance job runs unconditionally",
        )
    )

    secrets = _secret_names(document)
    result["secrets_referenced"] = secrets
    missing_secrets = [name for name in KUBECONFIG_SECRETS if name not in secrets]
    checks.append(
        _check(
            "workflow.kubeconfig_secrets",
            not missing_secrets,
            (
                "references " + ", ".join(secrets)
                if not missing_secrets
                else "missing reference(s): " + ", ".join(missing_secrets)
            ),
        )
    )

    result["ready"] = all(item["ok"] for item in checks)
    return result


def lab_workflow_report(repo_root: Path) -> dict[str, Any]:
    return inspect_lab_workflow(repo_root / WORKFLOW_PATH)


__all__ = [
    "ACCEPTANCE_JOB_NAMES",
    "KUBECONFIG_SECRETS",
    "REQUIRED_ACCEPTANCE_LABELS",
    "WORKFLOW_PATH",
    "inspect_lab_workflow",
    "lab_workflow_report",
]
