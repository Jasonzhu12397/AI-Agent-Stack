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
    _inventory_digest,
)


OBJECT = {
    "apiVersion": "cluster.x-k8s.io/v1beta1",
    "kind": "Cluster",
    "metadata": {
        "name": "target-cluster",
        "namespace": "metal3-system",
        "uid": "original-cluster-uid",
    },
    "spec": {
        "clusterNetwork": {"pods": {"cidrBlocks": ["10.244.0.0/16"]}},
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

    assert result["state"] == "simulation_ready"
    assert result["pivot_executed"] is False
    assert result["pivot_simulated"] is True
    assert result["execution_mode"] == "preflight_verified_simulation"
    assert result["dry_run_passed"] is True
    assert result["inventory_objects"] == 1
    assert len(result["inventory_sha256"]) == 64
    assert result["ownership_transferred"] is False
    assert result["side_effects"] is False
    assert result["target_agent_acknowledgement_required"] is True
    assert result["postflight_required"] is True
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


def test_target_uid_is_new_but_stable_object_identity_is_verified():
    replacement = {
        **OBJECT,
        "metadata": {**OBJECT["metadata"], "uid": "target-generated-uid"},
    }

    def runner(command, timeout):
        if command[0] == "kubectl":
            kubeconfig = command[command.index("--kubeconfig") + 1]
            if kubeconfig == "target.kubeconfig":
                return CommandResult(0, stdout=json.dumps(replacement))
            return CommandResult(
                1,
                stderr="Error from server (NotFound): clusters not found",
            )
        return CommandResult(0)

    assert verify_pivot_inventory(
        runner,
        "source.kubeconfig",
        "target.kubeconfig",
        INVENTORY,
    ) is True


def test_target_spec_mismatch_is_a_hard_verification_failure():
    replacement = {
        **OBJECT,
        "metadata": {**OBJECT["metadata"], "uid": "target-generated-uid"},
        "spec": {"clusterNetwork": {"pods": {"cidrBlocks": ["10.0.0.0/16"]}}},
    }

    def runner(command, timeout):
        if command[0] == "kubectl":
            return CommandResult(0, stdout=json.dumps(replacement))
        return CommandResult(0)

    with pytest.raises(PivotVerificationError, match="different spec"):
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


def test_dry_run_timeout_does_not_attempt_mutating_move():
    calls = []

    def runner(command, timeout):
        calls.append(list(command))
        raise subprocess.TimeoutExpired(command, timeout)

    with pytest.raises(PivotError, match="inspect both clusters"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            runner=runner,
        )

    assert len(calls) == 1
    assert calls[0][-1] == "--dry-run"


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


class MemoryPivotJournal:
    def __init__(self):
        self.records = {}

    def load_state(self, operation_id):
        return self.records.get(operation_id)

    def save_state(self, operation_id, record):
        self.records[operation_id] = dict(record)


def test_real_pivot_requires_acknowledgement_and_durable_journal():
    with pytest.raises(PivotError, match="durable journal"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            execute=True,
        )


def test_real_pivot_executes_once_and_verifies_source_and_target_ownership():
    runner = SuccessfulPivotRunner()
    journal = MemoryPivotJournal()
    result = pivot_cluster_api(
        "source.kubeconfig",
        "target.kubeconfig",
        "metal3-system",
        INVENTORY,
        runner=runner,
        execute=True,
        operation_id="pivot-operation-001",
        target_agent_ack=lambda payload: payload["operation_id"] == "pivot-operation-001",
        journal=journal,
        monotonic=lambda: 0.0,
    )

    moves = [
        command for command in runner.commands
        if command[0] == "clusterctl" and "--dry-run" not in command
    ]
    assert len(moves) == 1
    assert result["pivot_executed"] is True
    assert result["ownership_transferred"] is True
    assert journal.records["pivot-operation-001"]["state"] == "completed"


def test_ambiguous_pivot_journal_blocks_second_mutating_attempt():
    runner = SuccessfulPivotRunner()
    journal = MemoryPivotJournal()
    journal.save_state(
        "pivot-operation-ambiguous",
        {
            "state": "uncertain",
            "inventory_sha256": __import__(
                "worker_app.capi_pivot", fromlist=["_inventory_digest"]
            )._inventory_digest(tuple(INVENTORY)),
        },
    )

    with pytest.raises(PivotVerificationError, match="ambiguous prior execution"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            runner=runner,
            execute=True,
            operation_id="pivot-operation-ambiguous",
            target_agent_ack=lambda payload: True,
            journal=journal,
        )

    assert not any(
        command[0] == "clusterctl" and "--dry-run" not in command
        for command in runner.commands
    )


def test_real_pivot_refuses_inventory_from_another_namespace():
    with pytest.raises(ValueError, match="outside the requested namespace"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            [CapiObjectRef("Cluster", "foreign", "uid", "other-namespace")],
            execute=True,
            operation_id="pivot-operation-foreign",
            target_agent_ack=lambda payload: True,
            journal=MemoryPivotJournal(),
        )


def test_pivot_inventory_digest_binds_api_version_and_spec():
    changed = CapiObjectRef(
        kind=INVENTORY[0].kind,
        name=INVENTORY[0].name,
        uid=INVENTORY[0].uid,
        namespace=INVENTORY[0].namespace,
        api_version=INVENTORY[0].api_version,
        spec_sha256="f" * 64,
    )

    assert _inventory_digest(tuple(INVENTORY)) != _inventory_digest((changed,))


def test_target_identity_requires_api_server_generated_uid():
    replacement = {
        **OBJECT,
        "metadata": {key: value for key, value in OBJECT["metadata"].items() if key != "uid"},
    }

    def runner(command, timeout):
        if command[0] == "kubectl":
            return CommandResult(0, stdout=json.dumps(replacement))
        return CommandResult(0)

    with pytest.raises(PivotVerificationError, match="different stable identity"):
        verify_pivot_inventory(
            runner,
            "source.kubeconfig",
            "target.kubeconfig",
            INVENTORY,
        )


def test_interrupted_pivot_recovers_when_target_owns_inventory_and_source_is_empty():
    runner = SuccessfulPivotRunner()
    journal = MemoryPivotJournal()
    journal.save_state(
        "pivot-operation-recover",
        {
            "state": "verifying",
            "inventory_sha256": _inventory_digest(tuple(INVENTORY)),
        },
    )

    result = pivot_cluster_api(
        "source.kubeconfig",
        "target.kubeconfig",
        "metal3-system",
        INVENTORY,
        runner=runner,
        execute=True,
        operation_id="pivot-operation-recover",
        target_agent_ack=lambda payload: True,
        journal=journal,
    )

    assert result["state"] == "completed"
    assert result["recovered_after_interruption"] is True
    assert result["pivot_executed"] is True
    assert journal.records["pivot-operation-recover"]["state"] == "completed"
    assert not any(
        command[0] == "clusterctl" and "--dry-run" not in command
        for command in runner.commands
    )


def test_interrupted_pivot_does_not_retry_when_ownership_is_not_transferred():
    calls = []

    def runner(command, timeout):
        calls.append(list(command))
        if command[0] == "kubectl":
            return CommandResult(
                1,
                stderr="Error from server (NotFound): clusters not found",
            )
        return CommandResult(0)

    journal = MemoryPivotJournal()
    journal.save_state(
        "pivot-operation-incomplete",
        {
            "state": "executing",
            "inventory_sha256": _inventory_digest(tuple(INVENTORY)),
        },
    )

    with pytest.raises(PivotVerificationError, match="ownership is not fully transferred"):
        pivot_cluster_api(
            "source.kubeconfig",
            "target.kubeconfig",
            "metal3-system",
            INVENTORY,
            runner=runner,
            execute=True,
            operation_id="pivot-operation-incomplete",
            target_agent_ack=lambda payload: True,
            journal=journal,
        )

    assert not any(
        command[0] == "clusterctl" and "--dry-run" not in command
        for command in calls
    )
