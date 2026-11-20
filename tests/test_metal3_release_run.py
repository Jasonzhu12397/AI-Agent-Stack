import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from tools.metal3_release_run import run_pipeline

KINDS = {
    "01-preflight.json": "metal3-preflight",
    "02-redfish.json": "metal3-redfish-evidence",
    "03-cluster.json": "metal3-cluster-evidence",
    "04-acceptance.json": "metal3-acceptance",
}


def make_args(tmp_path: Path, **overrides) -> Namespace:
    values = {
        "project": "project-1",
        "kubeconfig": tmp_path / "kubeconfig",
        "target_kubeconfig": None,
        "control_plane_replicas": 3,
        "worker_replicas": 4,
        "evidence_dir": tmp_path / "evidence",
        "out": tmp_path / "evidence" / "release-manifest.json",
        "timeout": 10,
        "stage_timeout": 900,
        "skip_redfish": False,
        "commit": "abc123",
        "django_settings": "platform_api.settings",
    }
    values.update(overrides)
    return Namespace(**values)


def fake_runner_factory(fail_stage=None, skip_write=None):
    def runner(argv, **kwargs):
        out = Path(argv[argv.index("--out") + 1])
        if skip_write and skip_write in out.name:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        kind = KINDS[out.name]
        out.write_text(
            json.dumps(
                {
                    "kind": kind,
                    "version": "1",
                    "status": "pass",
                    "checks": [{"name": "ok", "status": "pass"}],
                }
            ),
            encoding="utf-8",
        )
        code = 1 if fail_stage and fail_stage in out.name else 0
        return SimpleNamespace(returncode=code, stdout="{}", stderr="")

    return runner


def test_pipeline_runs_every_stage_and_seals_a_passing_manifest(tmp_path):
    args = make_args(tmp_path)
    manifest, stages = run_pipeline(args, runner=fake_runner_factory())
    assert [stage["stage"] for stage in stages] == ["preflight", "redfish", "cluster", "verify"]
    assert all(stage["status"] == "pass" for stage in stages)
    assert manifest["status"] == "pass"
    assert manifest["commit"] == "abc123"
    assert manifest["problems"] == []
    assert len(manifest["evidence"]) == 4


def test_pipeline_fails_when_a_stage_exits_nonzero(tmp_path):
    args = make_args(tmp_path)
    manifest, _ = run_pipeline(args, runner=fake_runner_factory(fail_stage="03-cluster.json"))
    assert manifest["status"] == "fail"
    assert any("non-zero exit code" in item for item in manifest["problems"])


def test_pipeline_honours_skip_redfish(tmp_path):
    args = make_args(tmp_path, skip_redfish=True)
    manifest, stages = run_pipeline(args, runner=fake_runner_factory())
    assert [stage["stage"] for stage in stages] == ["preflight", "cluster", "verify"]
    assert "metal3-redfish-evidence" not in manifest["required_kinds"]
    assert manifest["status"] == "pass"


def test_pipeline_fails_closed_when_stage_evidence_is_missing(tmp_path):
    args = make_args(tmp_path)
    manifest, _ = run_pipeline(args, runner=fake_runner_factory(skip_write="02-redfish.json"))
    assert manifest["status"] == "fail"
    assert any("missing required evidence kind" in item for item in manifest["problems"])
