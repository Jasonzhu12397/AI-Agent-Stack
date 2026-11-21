from pathlib import Path

import pytest

from apps.eph_agent.control_stack_manifests import (
    ControlStackManifestError,
    load_component_manifests,
)


def test_loads_multi_document_manifest(tmp_path: Path):
    (tmp_path / "ipam.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: metal3-ipam-system\n"
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


def test_shipped_ipam_manifest_has_no_unexpanded_shell_placeholders():
    """Kubernetes does not expand ``${VAR:=default}`` inside container args.

    The vendored files stay byte-identical to their upstream release artifacts
    (and therefore still carry the placeholders), so the check is on the loader
    output: what would actually be applied must be fully expanded.
    """

    root = Path(__file__).resolve().parents[1]
    directory = root / "deploy/offline/addons/control-stack"
    names = sorted(path.stem for path in directory.glob("*.yaml"))
    documents = load_component_manifests(names, directory)

    assert set(documents) == set(names)
    found_args = 0
    for component, loaded in documents.items():
        for document in loaded:
            if document.get("kind") != "Deployment":
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                for argument in container.get("args", []):
                    found_args += 1
                    assert "${" not in argument, f"{component}: {argument}"
    assert found_args > 0
