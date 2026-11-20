import pytest

from apps.eph_agent.control_stack import install_order, plan_control_stack
from apps.eph_agent.metal3_skill import Metal3SkillError


def test_control_stack_plans_five_components_in_canonical_order():
    planned = plan_control_stack({})
    assert [component.name for component in planned] == install_order()
    assert [component.install_order for component in planned] == [1, 2, 3, 4, 5, 6]
    cert_manager = planned[0]
    assert cert_manager.namespace == "cert-manager"
    probe = cert_manager.readiness_probe()
    assert probe == {
        "kind": "Deployment",
        "name": "cert-manager",
        "namespace": "cert-manager",
    }


def test_pinned_versions_are_carried_through_and_missing_versions_omit():
    planned = plan_control_stack({"versions": {"capm3": "v1.13.0"}})
    by_name = {component.name: component for component in planned}
    assert by_name["capm3"].version == "v1.13.0"
    assert by_name["cert-manager"].version is None


def test_rejects_unknown_component_version_pin():
    with pytest.raises(Metal3SkillError, match="CONTROL_STACK_UNKNOWN_COMPONENT"):
        plan_control_stack({"versions": {"not-a-component": "v1.0.0"}})


def test_rejects_malformed_version():
    with pytest.raises(Metal3SkillError, match="CONTROL_STACK_VERSION_INVALID"):
        plan_control_stack({"versions": {"capm3": "1.13"}})


def test_rejects_non_mapping_spec():
    with pytest.raises(Metal3SkillError, match="CONTROL_STACK_SPEC_INVALID"):
        plan_control_stack(["list", "not", "dict"])


def test_ipam_probe_matches_the_vendored_release_manifest():
    """The probe must name the Deployment the vendored IPAM release creates."""

    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    manifest = root / "deploy/offline/addons/control-stack/ipam.yaml"
    deployments = [
        document
        for document in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
        if document and document.get("kind") == "Deployment"
    ]
    assert len(deployments) == 1
    names = {component.name: component for component in plan_control_stack({})}
    probe = names["ipam"].readiness_probe()
    assert probe["name"] == deployments[0]["metadata"]["name"]
    assert probe["namespace"] == deployments[0]["metadata"]["namespace"]


def test_control_stack_probes_agree_with_readiness_checks():
    """Two modules observe the same controllers; they must not disagree."""

    from apps.eph_agent import metal3_readiness

    providers = set(metal3_readiness.PROVIDER_DEPLOYMENTS)
    planned = [
        (component.namespace, component.controller)
        for component in plan_control_stack({})
    ]
    assert set(planned) & providers == {
        ("capm3-system", "capm3-controller-manager"),
        ("capm3-system", "ipam-controller-manager"),
        ("capi-system", "capi-controller-manager"),
        ("baremetal-operator-system", "baremetal-operator-controller-manager"),
    }
