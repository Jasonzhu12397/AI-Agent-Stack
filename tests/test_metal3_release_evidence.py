import json
from pathlib import Path

from tools.metal3_release_evidence import (
    build_manifest,
    main,
)


def write_evidence(directory: Path, name: str, kind: str, status: str, checks=None) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "kind": kind,
                "version": "1",
                "generated_at": "2026-11-20T00:00:00+00:00",
                "status": status,
                "checks": checks if checks is not None else [{"name": "ok", "status": "pass"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_release_manifest_passes_only_with_every_required_kind(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_evidence(evidence, "01-preflight.json", "metal3-preflight", "pass")
    write_evidence(evidence, "02-redfish.json", "metal3-redfish-evidence", "pass")
    write_evidence(evidence, "03-cluster.json", "metal3-cluster-evidence", "pass")
    manifest = build_manifest(evidence, commit="abc123")
    assert manifest["status"] == "pass"
    assert manifest["commit"] == "abc123"
    assert len(manifest["evidence"]) == 3
    assert all(len(item["sha256"]) == 64 for item in manifest["evidence"])
    assert manifest["problems"] == []


def test_release_manifest_fails_closed_when_evidence_is_missing(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_evidence(evidence, "01-preflight.json", "metal3-preflight", "pass")
    manifest = build_manifest(evidence)
    assert manifest["status"] == "fail"
    assert any("missing required evidence kind" in item for item in manifest["problems"])


def test_release_manifest_surfaces_failed_checks(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_evidence(evidence, "01-preflight.json", "metal3-preflight", "pass")
    write_evidence(evidence, "02-redfish.json", "metal3-redfish-evidence", "pass")
    write_evidence(
        evidence,
        "03-cluster.json",
        "metal3-cluster-evidence",
        "fail",
        [{"name": "nodes-ready", "status": "fail", "detail": "1 of 3 NotReady"}],
    )
    manifest = build_manifest(evidence)
    assert manifest["status"] == "fail"
    assert any("nodes-ready" in item for item in manifest["problems"])


def test_release_manifest_rejects_unknown_kind_and_bad_json(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "bad.json").write_text("{", encoding="utf-8")
    manifest = build_manifest(evidence)
    assert manifest["status"] == "fail"
    assert any("not valid JSON" in item for item in manifest["problems"])
    (evidence / "bad.json").unlink()
    write_evidence(evidence, "unknown.json", "metal3-unknown", "pass")
    manifest = build_manifest(evidence)
    assert manifest["status"] == "fail"
    assert any("unsupported evidence kind" in item for item in manifest["problems"])


def test_release_manifest_cli_exit_codes(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_evidence(evidence, "01-preflight.json", "metal3-preflight", "pass")
    write_evidence(evidence, "02-redfish.json", "metal3-redfish-evidence", "pass")
    write_evidence(evidence, "03-cluster.json", "metal3-cluster-evidence", "pass")
    out = evidence / "release-manifest.json"
    assert main(["--evidence-dir", str(evidence), "--out", str(out), "--commit", "abc"]) == 0
    assert json.loads(out.read_text())["status"] == "pass"
    write_evidence(evidence, "03-cluster.json", "metal3-cluster-evidence", "fail")
    assert main(["--evidence-dir", str(evidence), "--out", str(out), "--commit", "abc"]) == 1


def test_release_manifest_names_the_product_version(tmp_path):
    from packages.version import project_version

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_evidence(evidence, "01-preflight.json", "metal3-preflight", "pass")
    write_evidence(evidence, "02-redfish.json", "metal3-redfish-evidence", "pass")
    write_evidence(evidence, "03-cluster.json", "metal3-cluster-evidence", "pass")
    manifest = build_manifest(evidence, commit="abc")
    assert manifest["product_version"] == project_version()
    assert manifest["product_version"] != ""
