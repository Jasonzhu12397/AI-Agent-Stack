import tarfile
from pathlib import Path

import pytest

import tools.vendor_import as vendor_import

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "apps" / "eph_agent" / "metal3_lab.py"
README = ROOT / "deploy" / "metal3-lab" / "README.md"
VENDOR_README = ROOT / "deploy" / "metal3-lab" / "vendor" / "README.md"
VENDOR_DIR = ROOT / "deploy" / "metal3-lab" / "vendor" / "metal3-dev-env"


@pytest.fixture()
def fake_archive(tmp_path):
    """Build a minimal archive shaped like the GitHub source tarball."""

    source = tmp_path / "src" / "metal3-dev-env-abc123"
    (source / "lib").mkdir(parents=True)
    (source / "vm-setup").mkdir(parents=True)
    (source / ".github").mkdir(parents=True)
    for name in (
        "01_prepare_host.sh",
        "02_configure_host.sh",
        "03_launch_mgmt_cluster.sh",
        "04_verify.sh",
        "Makefile",
        "ansible.cfg",
    ):
        (source / name).write_text(f"# {name}\n", encoding="utf-8")
    (source / "lib" / "common.sh").write_text("# common\n", encoding="utf-8")
    (source / "vm-setup" / "inventory.ini").write_text("[all]\n", encoding="utf-8")
    (source / "vm-setup" / "install-package-playbook.yml").write_text(
        "- hosts: all\n", encoding="utf-8"
    )
    (source / "vm-setup" / "firewall.yml").write_text("- hosts: all\n", encoding="utf-8")
    # pruned by PRUNE_PATHS: a libvirt playbook and the VM-based e2e suite
    (source / "vm-setup" / "setup-playbook.yml").write_text("- hosts: all\n", encoding="utf-8")
    (source / "tests").mkdir(parents=True)
    (source / "tests" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (source / ".github" / "workflow.yml").write_text("name: ci\n", encoding="utf-8")

    archive = tmp_path / "upstream.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=source.name)
    return archive





def test_vendor_check_rejects_a_missing_tree(tmp_path):
    with pytest.raises(vendor_import.ImportError_):
        vendor_import.check_tree(tmp_path / "absent")


def test_vendor_import_extracts_the_pinned_tree_and_writes_provenance(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"

    metadata = vendor_import.import_tree(str(fake_archive), target)

    assert metadata["upstream_commit"] == vendor_import.PINNED_COMMIT
    assert metadata["file_count"] == 12
    assert metadata["retained_file_count"] == 8
    assert "vm-setup/setup-playbook.yml" in metadata["pruned_paths"]
    assert "tests" in metadata["pruned_paths"]
    assert (target / "01_prepare_host.sh").exists()
    assert (target / "lib" / "common.sh").exists()
    assert not (target / "vm-setup" / "setup-playbook.yml").exists()
    assert not (target / "tests").exists()
    assert not (target / ".github").exists()
    report = vendor_import.check_tree(target)
    assert report["problems"] == []
    assert "tests/run.sh" in report["pruned"]


def test_vendor_check_reports_local_edits_without_failing(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target)

    (target / "lib" / "common.sh").write_text("# locally patched\n", encoding="utf-8")

    assert vendor_import.check_tree(target)["problems"] == ["modified lib/common.sh"]


def test_vendor_check_rejects_a_mismatched_commit(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target, commit="deadbeef" * 5)

    with pytest.raises(vendor_import.ImportError_, match="does not match"):
        vendor_import.check_tree(target)


def test_vendor_check_rejects_an_incomplete_tree(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target)

    (target / "02_configure_host.sh").unlink()

    with pytest.raises(vendor_import.ImportError_, match="incomplete"):
        vendor_import.check_tree(target)


def test_vendor_import_rejects_path_traversal(tmp_path):
    malicious = tmp_path / "malicious.tar.gz"
    payload = tmp_path / "evil.txt"
    payload.write_text("x", encoding="utf-8")
    with tarfile.open(malicious, "w:gz") as handle:
        handle.add(payload, arcname="../evil.txt")

    with pytest.raises(vendor_import.ImportError_, match="escapes"):
        vendor_import.import_tree(str(malicious), tmp_path / "vendor" / "x")


def test_vendor_import_reports_a_missing_archive(tmp_path):
    with pytest.raises(vendor_import.ImportError_, match="archive not found"):
        vendor_import.import_tree(str(tmp_path / "missing.tar.gz"), tmp_path / "vendor" / "x")


def test_docs_distinguish_provider_e2e_from_agent_e2e():
    docs = README.read_text(encoding="utf-8")
    assert "three-agent" in docs
    assert "end-to-end test" in docs
    assert "LIBVIRT_IRONIC" in docs
    assert "Cloud Control Agent" in docs


def test_vendor_docs_explain_git_free_ownership():
    docs = VENDOR_README.read_text(encoding="utf-8")
    assert "repository code" in docs
    assert "no git command" in docs
    assert "VENDOR.json" in docs
    assert "MANIFEST.sha256" in docs


def test_vendored_tree_matches_its_manifest_when_present():
    if not VENDOR_DIR.exists():
        pytest.skip("vendored Metal3 tree is not materialised in this checkout")
    report = vendor_import.check_tree(VENDOR_DIR)
    metadata = report["metadata"]
    assert metadata["upstream_commit"] == vendor_import.PINNED_COMMIT
    # the tree is a fork now: upstream's virtual half, VM e2e suite and developer
    # tooling are deleted, so only the physical-machine path remains
    assert metadata["file_count"] < 60
    assert metadata["retained_file_count"] == metadata["file_count"]
    assert report["problems"] == []
    assert "vm-setup/roles/libvirt" in metadata["pruned_paths"]
    assert "tests" in metadata["pruned_paths"]
    for pruned in ("vm-setup/roles/libvirt", "vm-setup/roles/virtbmc", "vbmc.sh", "tests"):
        assert not (VENDOR_DIR / pruned).exists()


def test_vendor_check_rejects_a_pruned_path_that_came_back(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target)

    (target / "vm-setup" / "setup-playbook.yml").write_text("- hosts: all\n", encoding="utf-8")

    problems = vendor_import.check_tree(target)["problems"]
    assert any(
        problem.startswith("unexpected vm-setup/setup-playbook.yml") for problem in problems
    )


def test_reseal_records_the_fork_as_the_baseline(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target)
    (target / "lib" / "common.sh").write_text(
        "# rewritten for physical machines\n", encoding="utf-8"
    )

    assert vendor_import.check_tree(target)["problems"] == ["modified lib/common.sh"]

    metadata = vendor_import.reseal_tree(target)

    assert metadata["upstream_commit"] == vendor_import.PINNED_COMMIT
    assert metadata["resealed_at"]
    assert "tests" in metadata["pruned_paths"]
    assert vendor_import.check_tree(target)["problems"] == []


def test_reseal_refuses_a_tree_that_keeps_pruned_code(fake_archive, tmp_path):
    target = tmp_path / "vendor" / "metal3-dev-env"
    vendor_import.import_tree(str(fake_archive), target)
    (target / "vm-setup" / "roles" / "libvirt").mkdir(parents=True)

    with pytest.raises(vendor_import.ImportError_, match="must not be vendored"):
        vendor_import.reseal_tree(target)


