"""The upgrade contract is executed by the Target Agent, not only the API."""

import pytest
from agent_contracts.kubernetes_upgrade import (
    KubernetesUpgradeError,
    minor_of,
    simulation_plan,
    validate_request,
)


def _payload(**overrides):
    base = {
        "cluster_name": "edge-lab",
        "current_version": "v1.32.2",
        "target_version": "v1.33.0",
        "execution_mode": "simulation",
    }
    base.update(overrides)
    return base


def test_a_supported_adjacent_minor_upgrade_is_accepted():
    request = validate_request(_payload(), cluster_name="edge-lab")

    assert request["target_version"] == "v1.33.0"


def test_the_cluster_name_must_match_the_agent_cluster():
    with pytest.raises(KubernetesUpgradeError) as excinfo:
        validate_request(_payload(), cluster_name="other-cluster")

    assert excinfo.value.code == "CLUSTER_MISMATCH"


def test_skipping_a_minor_is_rejected():
    with pytest.raises(KubernetesUpgradeError) as excinfo:
        validate_request(_payload(target_version="v1.34.1"))

    assert excinfo.value.code == "VERSION_STEP_INVALID"


def test_an_unsupported_minor_is_rejected():
    with pytest.raises(KubernetesUpgradeError) as excinfo:
        validate_request(_payload(current_version="v1.36.2", target_version="v1.37.0"))

    assert excinfo.value.code == "VERSION_UNSUPPORTED"


def test_execution_mode_must_be_simulation():
    with pytest.raises(KubernetesUpgradeError) as excinfo:
        validate_request(_payload(execution_mode="apply"))

    assert excinfo.value.code == "EXECUTION_MODE_UNSUPPORTED"


def test_extra_payload_keys_are_rejected():
    with pytest.raises(KubernetesUpgradeError) as excinfo:
        validate_request(_payload(drain=True))

    assert excinfo.value.code == "PAYLOAD_INVALID"


def test_the_simulated_plan_never_claims_execution():
    plan = simulation_plan(validate_request(_payload()))

    assert plan["upgrade_simulated"] is True
    assert plan["upgrade_performed"] is False
    assert plan["requires_manual_execution"] is True
    assert plan["resume_supported"] is False
    assert plan["approval_required"] is True
    assert [step["name"] for step in plan["upgrade_steps"]] == [
        "preflight",
        "control_plane",
        "workers",
        "postflight",
    ]
    assert [step["status"] for step in plan["upgrade_steps"]] == [
        "not_run",
        "not_started",
        "not_started",
        "not_started",
    ]


def test_minor_of_keeps_the_leading_v():
    assert minor_of("v1.34.7") == "v1.34"


def test_execute_mode_is_a_valid_request_mode():
    request = validate_request(_payload(execution_mode="execute"))

    assert request["execution_mode"] == "execute"
