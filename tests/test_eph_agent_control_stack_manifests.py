from pathlib import Path

import pytest

from apps.eph_agent.control_stack_manifests import (
    ControlStackManifestError,
    load_component_manifests,
)


def test_loads_multi_document_manifest(tmp_path: Path):
    (tmp_path / "ipam.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: capm3-system\n"
        "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: ipam-manager\n",
        encoding="utf-8",
    )
    documents = load_component_manifests(["ipam", "cert-manager"], tmp_path)
    assert list(documents) == ["ipam"]
    assert [doc["kind"] for doc in documents["ipam"]] == ["Namespace", "Deployment"]


def test_missing_component_is_omitted(tmp_path: Path):
    assert load_component_manifests(["ironic"], tmp_path) == {}


def test_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(ControlStackManifestError) as excinfo:
        load_component_manifests(["../etc/passwd"], tmp_path)
    assert excinfo.value.code == "CONTROL_STACK_MANIFEST_NAME_INVALID"


def test_rejects_empty_manifest(tmp_path: Path):
    (tmp_path / "empty.yaml").write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(ControlStackManifestError) as excinfo:
        load_component_manifests(["empty"], tmp_path)
    assert excinfo.value.code == "CONTROL_STACK_MANIFEST_EMPTY"
