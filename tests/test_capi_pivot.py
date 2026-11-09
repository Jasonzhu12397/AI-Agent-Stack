# ruff: noqa: I001
import json
import subprocess

import pytest
from worker_app.capi_pivot import (
    CapiObjectRef,
    CommandResult,
    PivotCommandError,
    PivotError,
    PivotVerificationError,
    pivot_cluster_api,
    verify_pivot_inventory,
)


OBJECT = {
    "apiVersion": "cluster.x-k8s.io/v1beta1",
    "kind": "Cluster",
    "metadata": {
        "name": "target-cluster",
        "namespace": "metal3-system",
        "uid": "original-cluster-uid",
    },
}
INVENTORY = [CapiObjectRef.from_dict(OBJECT)]


class SuccessfulPivotRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(list(command))
        if command[0] == "clusterctl":
            return CommandResult(0, stdout="dry run or move completed")
        kubeconfig = command[command.index("--kubeconfig") + 1]
        if kubeconfig == "target.kubeconfig":
            return CommandResult(0, stdout=json.dumps(OBJECT))
        return CommandResult(1, stderr="Error from server (NotFound): clusters not found")


def test_pivot_runs_only_dry_run_and_returns_simulation_result():
    runner = SuccessfulPivotRunner()

    result = pivot_cluster_api(
        "source.kubeconfig",
        "target.kubeconfig",
        "metal3-system",
        INVENTORY,
        runner=runner,
    )

    assert result == {
        "state": "simulation_ready",
        "pivot_executed": False,
        "pivot_simulated": True,
        "execution_mode": "preflight_verified_simulation",
        "simulation_status": "ready_for_target_agent_acknowledgement",
        "dry_run_passed": True,
        "inventory_objects": 1,
        "ownership_transferred": False,
        "side_effects": False,
        "target_agent_acknowledgement_required": True,
        "postflight_required": True,
    }
    assert len(runner.commands) == 1
    assert runner.commands[0][0:2] == ["clusterctl", "move"]
    assert runner.commands[0][-1] == "--dry-run"


def test_dry_run_failure_never_executes_real_pivot():
    def runner(command, timeout):
        if command[0] == "clusterctl":
            return CommandResult(1, stderr="dry-run rejected")
        raise AssertionError("real pivot must not run after dry-run failure")

    with pytest.raises(PivotCommandError, match="dry-run rejected"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            runner=runner,
        )


def test_target_uid_mismatch_is_a_hard_verification_failure():
    replacement = {
        **OBJECT,
        "metadata": {**OBJECT["metadata"], "uid": "replacement-uid"},
    }

    def runner(command, timeout):
        if command[0] == "kubectl":
            return CommandResult(0, stdout=json.dumps(replacement))
        return CommandResult(0)

    with pytest.raises(PivotVerificationError, match="different UID"):
        verify_pivot_inventory(
            runner,
            "source.kubeconfig",
            "target.kubeconfig",
            INVENTORY,
        )


def test_source_object_still_present_means_pivot_is_not_verified():
    def runner(command, timeout):
        if command[0] == "kubectl":
            return CommandResult(0, stdout=json.dumps(OBJECT))
        return CommandResult(0)

    assert (
        verify_pivot_inventory(
            runner,
            "source.kubeconfig",
            "target.kubeconfig",
            INVENTORY,
        )
        is False
    )


def test_move_timeout_is_never_automatically_retried():
    calls = []

    def runner(command, timeout):
        calls.append(list(command))
        if command[0] == "clusterctl" and "--dry-run" in command:
            return CommandResult(0)
        raise subprocess.TimeoutExpired(command, timeout)

    with pytest.raises(PivotError, match="inspect both clusters"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            runner=runner,
        )

    assert len(calls) == 2
    assert calls[0][-1] == "--dry-run"
    assert "--dry-run" not in calls[1]


def test_pivot_rejects_same_kubeconfig_and_duplicate_inventory():
    with pytest.raises(ValueError, match="must be different"):
        pivot_cluster_api("same", "same", "metal3-system", INVENTORY)

    with pytest.raises(ValueError, match="duplicate"):
        pivot_cluster_api(
            "source",
            "target",
            "metal3-system",
            [INVENTORY[0], INVENTORY[0]],
        )
