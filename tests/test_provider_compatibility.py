import pytest

from node_agent import agent
from node_agent.provider_compatibility import (
    ProviderCompatibilityError,
    evaluate_provider_compatibility,
)


def payload(capm3="1.14.0", bmo="0.14.0", ironic="37.0.0", irso="0.12.0"):
    return {
        "provider_versions": {
            "capm3": capm3,
            "bmo": bmo,
            "ironic": ironic,
            "irso": irso,
        },
        "kubernetes_version": "v1.32.2",
        "capi_api_version": "v1beta2",
    }


def test_upstream_ci_profile_is_recognized_without_claiming_local_provisioning():
    result = evaluate_provider_compatibility(payload())

    assert result["compatibility_status"] == "supported"
    assert result["upstream_ci_profile_tested"] is True
    assert result["irso_ironic_compatible"] is True
    assert result["capi_api_compatible"] is True
    assert result["mutation_authorized"] is False
    assert result["provisioning_ready"] is False


def test_newer_supported_component_combination_is_not_mislabeled_as_ci_tested():
    result = evaluate_provider_compatibility(
        payload(ironic="39.0.0", irso="0.12.0")
    )

    assert result["compatibility_status"] == "supported"
    assert result["upstream_ci_profile_tested"] is False
    assert "provider_combination_not_listed_in_upstream_periodic_ci_matrix" in result["warnings"]


def test_irso_ironic_incompatible_pair_blocks_compatibility():
    result = evaluate_provider_compatibility(
        payload(ironic="35.0.0", irso="0.12.0")
    )

    assert result["compatibility_status"] == "unsupported"
    assert result["irso_ironic_compatible"] is False
    assert any(item.startswith("irso_ironic_version_pair_not_supported:") for item in result["blockers"])


def test_capm3_capi_api_mismatch_blocks_compatibility():
    result = evaluate_provider_compatibility(
        {
            **payload(),
            "capi_api_version": "v1beta1",
        }
    )

    assert result["compatibility_status"] == "unsupported"
    assert result["capi_api_compatible"] is False


def test_tested_but_not_currently_supported_releases_are_labeled_tested():
    result = evaluate_provider_compatibility(
        payload(capm3="1.12.0", bmo="0.12.0", ironic="33.0.0", irso="0.9.0")
    )

    assert result["compatibility_status"] == "tested"
    assert result["upstream_ci_profile_tested"] is True
    assert result["mutation_authorized"] is False


@pytest.mark.parametrize(
    "versions",
    [
        {},
        {"capm3": "1.14", "bmo": "0.14", "ironic": "39.0"},
        {"capm3": "main", "bmo": "0.14", "ironic": "39.0", "irso": "0.12"},
    ],
)
def test_invalid_or_incomplete_provider_versions_are_rejected(versions):
    with pytest.raises(ProviderCompatibilityError):
        evaluate_provider_compatibility({"provider_versions": versions})


def test_unknown_payload_fields_are_rejected():
    with pytest.raises(ProviderCompatibilityError, match="PROVIDER_COMPATIBILITY_PAYLOAD_INVALID"):
        evaluate_provider_compatibility(
            {
                **payload(),
                "bmc_password": "never-accept-secrets",
            }
        )



def test_node_agent_compatibility_command_uses_live_kubernetes_and_capi_api(monkeypatch):
    def fake_get(path):
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta2"},
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {
            "command_type": "cluster.provider.compatibility",
            "payload": {
                "provider_versions": {
                    "capm3": "1.14.0",
                    "bmo": "0.14.0",
                    "ironic": "37.0.0",
                    "irso": "0.12.0",
                }
            },
        },
        "eph_node",
    )

    assert result["compatibility_status"] == "supported"
    assert result["kubernetes_version"] == "v1.32.2"
    assert result["capi_api_version"] == "v1beta2"
    assert result["upstream_ci_profile_tested"] is True
    assert result["mutation_authorized"] is False


def test_node_agent_compatibility_command_is_not_enabled_on_target_role():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {
                "command_type": "cluster.provider.compatibility",
                "payload": {
                    "provider_versions": {
                        "capm3": "1.14.0",
                        "bmo": "0.14.0",
                        "ironic": "37.0.0",
                        "irso": "0.12.0",
                    }
                },
            },
            "target_cluster",
        )
