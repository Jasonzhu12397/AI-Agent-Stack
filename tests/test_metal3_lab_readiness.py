"""The lab workflow reports readiness instead of failing on missing secrets."""

import argparse
import base64
import json
from pathlib import Path

import yaml

from tools.metal3_lab_readiness import build_report, main

PROJECT = "9d0f6c2e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


def _kubeconfig(server="https://10.130.60.10:6443"):
    authority = base64.b64encode(b"ca-bytes").decode()
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "lab",
                    "cluster": {
                        "server": server,
                        "certificate-authority-data": authority,
                    },
                }
            ],
            "contexts": [{"name": "lab", "context": {"cluster": "lab", "user": "lab"}}],
            "users": [{"name": "lab", "user": {"token": "redacted"}}],
        }
    )


def _arguments(tmp_path: Path, **overrides):
    eph = tmp_path / "eph.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    eph.write_text(_kubeconfig(), encoding="utf-8")
    target.write_text(_kubeconfig("https://10.130.60.11:6443"), encoding="utf-8")
    values = {
        "project_id": PROJECT,
        "control_plane_replicas": "3",
        "worker_replicas": "4",
        "eph_kubeconfig": str(eph),
        "target_kubeconfig": str(target),
        "acceptance_tool": "tools/metal3_acceptance.py",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_ready_report_names_what_it_cannot_check(tmp_path):
    report = build_report(_arguments(tmp_path))
    assert report["ready"] is True
    assert report["failed_checks"] == []
    assert report["skipped_stages"] == []
    assert any("reachability" in item for item in report["not_checked"])
    assert "not evidence" in report["note"]


def test_missing_secret_skips_stages_instead_of_failing(tmp_path):
    report = build_report(_arguments(tmp_path, eph_kubeconfig=""))
    assert report["ready"] is False
    assert "eph.present" in report["failed_checks"]
    assert report["skipped_stages"]


def test_invalid_inputs_are_reported(tmp_path):
    report = build_report(
        _arguments(tmp_path, project_id="not-a-uuid", worker_replicas="0")
    )
    assert report["failed_checks"] == ["project_id", "worker_replicas"]


def test_kubeconfig_without_cluster_is_reported(tmp_path):
    broken = tmp_path / "broken.kubeconfig"
    broken.write_text("clusters: []\ncontexts: []\nusers: []\n", encoding="utf-8")
    report = build_report(_arguments(tmp_path, target_kubeconfig=str(broken)))
    assert "target.clusters" in report["failed_checks"]
    assert "target.server" in report["failed_checks"]


def test_bad_base64_authority_is_reported(tmp_path):
    bad = tmp_path / "bad-ca.kubeconfig"
    document = yaml.safe_load(_kubeconfig())
    document["clusters"][0]["cluster"]["certificate-authority-data"] = "not base64!"
    bad.write_text(yaml.safe_dump(document), encoding="utf-8")
    report = build_report(_arguments(tmp_path, eph_kubeconfig=str(bad)))
    assert "eph.ca" in report["failed_checks"]


def test_cli_writes_report_and_github_output_without_failing(tmp_path, capsys):
    out = tmp_path / "evidence" / "lab-readiness.json"
    github_output = tmp_path / "github-output.txt"
    exit_code = main(
        [
            "--project",
            PROJECT,
            "--control-plane-replicas",
            "3",
            "--worker-replicas",
            "4",
            "--eph-kubeconfig",
            "",
            "--target-kubeconfig",
            "",
            "--out",
            str(out),
            "--github-output",
            str(github_output),
        ]
    )
    assert exit_code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["ready"] is False
    assert "will be skipped" in capsys.readouterr().out
    assert github_output.read_text(encoding="utf-8").strip() == "ready=false"
