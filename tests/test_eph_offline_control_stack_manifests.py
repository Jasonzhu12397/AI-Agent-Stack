"""Every planned control-stack component must ship a real offline manifest.

The EPH applies vendored multi-document manifests from
``deploy/offline/addons/control-stack``. If a component has no file the adapter
silently records ``apply_pending`` and the bootstrap never becomes ready, so the
plan and the shipped files are verified against each other here.
"""

from pathlib import Path

import yaml

from apps.eph_agent.control_stack import plan_control_stack
from apps.eph_agent.control_stack_manifests import (
    MAX_MANIFEST_BYTES,
    load_component_manifests,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO_ROOT / "deploy/offline/addons/control-stack"


def _documents(name: str) -> list[dict]:
    loaded = list(
        yaml.safe_load_all((MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    )
    return [item for item in loaded if isinstance(item, dict) and item]


def test_every_planned_component_ships_a_manifest():
    plan = plan_control_stack({})
    missing = [
        component.name
        for component in plan
        if not (MANIFEST_DIR / f"{component.name}.yaml").is_file()
    ]
    assert missing == []


def test_manifest_loader_accepts_every_shipped_file():
    plan = plan_control_stack({})
    documents = load_component_manifests(
        [component.name for component in plan],
        MANIFEST_DIR,
    )
    assert sorted(documents) == sorted(component.name for component in plan)
    for name, docs in documents.items():
        assert docs, name


def test_shipped_files_stay_below_the_loader_limit():
    for component in plan_control_stack({}):
        path = MANIFEST_DIR / f"{component.name}.yaml"
        assert path.stat().st_size <= MAX_MANIFEST_BYTES, component.name


def test_readiness_probe_deployments_exist_in_the_shipped_manifests():
    for component in plan_control_stack({}):
        deployments = {
            (document["metadata"].get("namespace"), document["metadata"]["name"])
            for document in _documents(component.name)
            if document.get("kind") == "Deployment"
        }
        assert (
            component.namespace,
            component.controller,
        ) in deployments, component.name


def test_ironic_custom_resource_is_declared_by_its_manifest():
    ironic = {item.name: item for item in plan_control_stack({})}["ironic"]
    probe = ironic.custom_resource_probe()
    assert probe is not None
    served = {
        (version["name"], document["spec"]["group"], document["spec"]["names"]["kind"])
        for document in _documents("ironic")
        if document.get("kind") == "CustomResourceDefinition"
        for version in document["spec"]["versions"]
    }
    assert any(
        f"{group}/{version}" == probe["api_version"] and kind == probe["kind"]
        for version, group, kind in served
    )
