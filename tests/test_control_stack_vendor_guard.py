"""The vendored control stack must match what the code claims it installs.

Every file here is a release artifact from a different upstream project, so the
drift that hurts is silent: a file changed in place, an image left on a mutable
tag, a component vendored from an older release train than the provider that
uses it, or a Deployment name the install plan waits for that the artifact does
not create. Each of those has been a real failure mode, and each is checked
here against the artifacts themselves rather than against a list in a test.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from eph_agent import control_stack, metal3_readiness

VENDOR_DIR = (
    Path(__file__).resolve().parents[1] / "deploy/offline/addons/control-stack"
)
README = VENDOR_DIR / "README.md"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _documents(name: str) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all((VENDOR_DIR / name).read_text())
        if document
    ]


def test_every_vendored_file_matches_the_hash_published_beside_it():
    readme = README.read_text()
    published = dict(re.findall(r"\| `(.+?)` \|.+?\| `([0-9a-f]{64})` \|", readme))

    assert published, "the README must publish a SHA256 for every artifact"
    for name, expected in published.items():
        digest = hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} does not match its published hash"


def test_what_gets_applied_has_no_unexpanded_placeholders():
    """The files keep upstream placeholders; the loader must remove them."""

    from eph_agent.control_stack_manifests import load_component_manifests

    names = sorted(path.stem for path in VENDOR_DIR.glob("*.yaml"))
    documents = load_component_manifests(names, VENDOR_DIR)
    for component, loaded in documents.items():
        for document in loaded:
            if document.get("kind") != "Deployment":
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                for argument in container.get("args", []) + container.get("command", []):
                    assert "${" not in argument, f"{component}: {argument}"


def test_ipam_tracks_the_capm3_release_train():
    """CAPM3 v1.14 uses IPPool.allocationStrategy, so IPAM must too."""

    documents = _documents("ipam.yaml")
    ippool = next(
        document
        for document in documents
        if document.get("kind") == "CustomResourceDefinition"
        and document["metadata"]["name"].startswith("ippools")
    )
    spec = ippool["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert "allocationStrategy" in spec["properties"], (
        "the vendored IPPool schema is older than CAPM3 and would prune the field"
    )


def test_the_ipam_image_is_pinned_by_digest():
    deployment = next(
        document
        for document in _documents("ipam.yaml")
        if document.get("kind") == "Deployment"
    )
    images = [
        container["image"]
        for container in deployment["spec"]["template"]["spec"]["containers"]
    ]
    assert images and all(DIGEST.search(image) for image in images), images


def test_the_install_plan_names_deployments_that_really_exist():
    plan = control_stack._COMPONENTS
    for component, namespace, deployment, _custom in plan:
        documents = _documents(f"{component}.yaml")
        found = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document["metadata"]["name"] == deployment
        ]
        assert found, f"{component}.yaml does not create Deployment {deployment}"
        assert found[0]["metadata"].get("namespace") == namespace, (
            f"{component}.yaml puts {deployment} in "
            f"{found[0]['metadata'].get('namespace')}, the plan says {namespace}"
        )


def test_readiness_probes_the_same_deployments_as_the_install_plan():
    planned = {
        (namespace, deployment)
        for _component, namespace, deployment, _custom in control_stack._COMPONENTS
    }
    for namespace, deployment in metal3_readiness.PROVIDER_DEPLOYMENTS:
        if namespace.startswith("ai-agent-system"):
            # EPH-local services, not part of the vendored control stack.
            continue
        assert (namespace, deployment) in planned, (namespace, deployment)
