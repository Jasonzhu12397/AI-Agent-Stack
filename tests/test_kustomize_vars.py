"""Upstream release artifacts ship kustomize placeholders; expanding is a step."""

import pytest
from agent_contracts.kustomize_vars import KustomizeVarError, expand_kustomize_vars


def test_a_placeholder_without_an_override_uses_its_own_default():
    text = "args:\n  - --diagnostics-address=${CAPI_DIAGNOSTICS_ADDRESS:=:8443}\n"

    assert "--diagnostics-address=:8443" in expand_kustomize_vars(text)


def test_an_override_wins_over_the_default():
    text = "- --diagnostics-address=${CAPI_DIAGNOSTICS_ADDRESS:=:8443}"

    assert expand_kustomize_vars(text, {"CAPI_DIAGNOSTICS_ADDRESS": ":9443"}) == (
        "- --diagnostics-address=:9443"
    )


def test_several_placeholders_in_one_argument_are_all_expanded():
    text = (
        "- --feature-gates=MachinePool=${EXP_MACHINE_POOL:=true},"
        "ClusterTopology=${CLUSTER_TOPOLOGY:=false},RuntimeSDK=${EXP_RUNTIME_SDK:=false}"
    )

    assert expand_kustomize_vars(text) == (
        "- --feature-gates=MachinePool=true,ClusterTopology=false,RuntimeSDK=false"
    )


def test_a_placeholder_without_a_default_is_never_applied_as_literal_text():
    with pytest.raises(KustomizeVarError) as excinfo:
        expand_kustomize_vars("image: ${IRONIC_IMAGE}")

    assert excinfo.value.code == "KUSTOMIZE_VAR_UNEXPANDED"
    assert excinfo.value.names == ("IRONIC_IMAGE",)


def test_a_placeholder_without_a_default_accepts_an_override():
    assert expand_kustomize_vars("image: ${IRONIC_IMAGE}", {"IRONIC_IMAGE": "x"}) == "image: x"


def test_an_empty_default_is_allowed_and_text_outside_braces_is_untouched():
    text = "value: ${MAYBE:=}\nnote: keep ${not-a-var} as is"

    assert expand_kustomize_vars(text) == "value: \nnote: keep ${not-a-var} as is"
