"""A rollback restores data, keeps the old one, and refuses half a quorum."""

import json

import pytest
from agent_contracts.kubernetes_rollback import (
    KubernetesRollbackError,
    planned_steps,
    simulation_plan,
    validate_request,
)
from target_agent.kubernetes_rollback_exec import (
    RollbackExecutionError,
    RollbackExecutor,
)

REQUEST = {
    "cluster_name": "edge-lab",
    "snapshot_path": "/var/lib/etcd/backups/snapshot-pre-v1.34.1.db",
    "execution_mode": "execute",
    "confirm_cluster_name": "edge-lab",
}


def _nodes(*versions):
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": f"cp-{index}",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "nodeInfo": {"kubeletVersion": version},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
                for index, version in enumerate(versions)
            ]
        }
    )


def _etcd_pods(*names):
    return json.dumps(
        {
            "items": [
                {"metadata": {"name": name}, "spec": {"nodeName": "cp-0"}}
                for name in names
            ]
        }
    )


class RollbackRunner:
    def __init__(
        self,
        *,
        nodes=None,
        pods=None,
        fail_on=(),
        api_after_start=True,
        snapshot_status='{"hash":"abc","revision":42}',
    ):
        self.calls = []
        self._nodes = nodes or _nodes("v1.34.1")
        self._pods = pods or _etcd_pods("etcd-cp-0")
        self._fail_on = tuple(fail_on)
        self._api_after_start = api_after_start
        self._kubelet_started = False
        self._snapshot_status = snapshot_status

    def __call__(self, argv, timeout=20, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for marker in self._fail_on:
            if marker in joined:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "boom"}
        if argv[:2] == ["kubectl", "get"] and "nodes" in argv:
            if self._kubelet_started and not self._api_after_start:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "refused"}
            return {"available": True, "returncode": 0, "stdout": self._nodes, "stderr": ""}
        if "component=etcd" in joined:
            return {"available": True, "returncode": 0, "stdout": self._pods, "stderr": ""}
        if "snapshot" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": self._snapshot_status,
                "stderr": "",
            }
        if "health" in argv:
            return {"available": True, "returncode": 0, "stdout": "healthy", "stderr": ""}
        if argv[:3] == ["sudo", "systemctl", "start"]:
            self._kubelet_started = True
        return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}


def test_the_simulated_plan_lists_five_unexecuted_stages():
    plan = simulation_plan(validate_request(REQUEST))

    assert plan["rollback_simulated"] is True
    assert plan["rolled_back"] is False
    assert plan["performed_steps"] == []
    assert [step["name"] for step in plan["steps"]] == [
        step["name"] for step in planned_steps(REQUEST)
    ]
    assert all(step["status"] == "not_started" for step in plan["steps"])


def test_a_single_control_plane_is_restored_and_the_old_data_kept():
    runner = RollbackRunner()
    result = RollbackExecutor(runner, api_wait_attempts=1).run(REQUEST)

    assert result["failure_code"] is None
    assert result["rolled_back"] is True
    assert result["performed_steps"] == [
        "etcd_restore",
        "data_swap",
        "kubelet_restart",
        "postflight",
    ]
    assert result["previous_member_dir"].startswith("/var/lib/etcd/member.pre-rollback-")
    assert result["restore_dir"].startswith("/var/lib/etcd/rollback-")
    assert result["steps"][0]["detail"].endswith("checks passed")
    swap = [call for call in runner.calls if call[:2] == ["sudo", "mv"]]
    assert swap[0][2] == "/var/lib/etcd/member"
    assert swap[0][3] == result["previous_member_dir"]
    assert swap[1][2] == f"{result['restore_dir']}/member"
    order = [i for i, call in enumerate(runner.calls) if call[:2] == ["sudo", "mv"]]
    stop = next(
        i for i, call in enumerate(runner.calls)
        if call[:3] == ["sudo", "systemctl", "stop"]
    )
    assert stop < order[0]


def test_a_multi_control_plane_cluster_is_refused_before_anything_is_touched():
    runner = RollbackRunner(nodes=_nodes("v1.34.1", "v1.34.1", "v1.34.1"))

    with pytest.raises(RollbackExecutionError) as raised:
        RollbackExecutor(runner).run(REQUEST)
    assert raised.value.code == "ROLLBACK_MULTI_CONTROL_PLANE_UNSUPPORTED"
    assert not [call for call in runner.calls if call[:1] == ["sudo"]]


def test_a_missing_snapshot_blocks_before_restore():
    runner = RollbackRunner(fail_on=("test -f",))
    result = RollbackExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_PREFLIGHT_BLOCKED"
    assert result["rolled_back"] is False
    assert result["performed_steps"] == []
    assert any(
        check["name"] == "etcd.snapshot.exists" and not check["ok"]
        for check in result["preflight"]["checks"]
    )
    assert not [call for call in runner.calls if "restore" in call]


def test_an_unverified_snapshot_blocks_before_restore():
    runner = RollbackRunner(snapshot_status="not json")
    result = RollbackExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_PREFLIGHT_BLOCKED"
    assert any(
        check["name"] == "etcd.snapshot.verified" and not check["ok"]
        for check in result["preflight"]["checks"]
    )


def test_a_kubelet_that_will_not_stop_never_swaps_the_data():
    runner = RollbackRunner(fail_on=("systemctl stop",))
    result = RollbackExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_KUBELET_STOP_FAILED"
    assert result["performed_steps"] == ["etcd_restore"]
    assert not [call for call in runner.calls if call[:2] == ["sudo", "mv"]]


def test_api_that_never_returns_fails_the_rollback_and_names_the_kept_data():
    runner = RollbackRunner(api_after_start=False)
    result = RollbackExecutor(runner, api_wait_attempts=1).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_API_UNAVAILABLE"
    assert result["steps"][3]["status"] == "failed"
    assert result["previous_member_dir"].startswith("/var/lib/etcd/member.pre-rollback-")


def test_the_request_refuses_what_it_cannot_do_safely():
    assert validate_request(REQUEST)["execution_mode"] == "execute"
    with pytest.raises(KubernetesRollbackError) as raised:
        validate_request({**REQUEST, "confirm_cluster_name": ""})
    assert raised.value.code == "ROLLBACK_CONFIRMATION_MISMATCH"
    with pytest.raises(KubernetesRollbackError) as raised:
        validate_request({**REQUEST, "snapshot_path": "../../etc/shadow.db"})
    assert raised.value.code == "ROLLBACK_SNAPSHOT_PATH_INVALID"
    with pytest.raises(KubernetesRollbackError) as raised:
        validate_request({**REQUEST, "cluster_name": "other-cluster"}, cluster_name="edge-lab")
    assert raised.value.code == "ROLLBACK_CLUSTER_NAME_MISMATCH"
    with pytest.raises(KubernetesRollbackError) as raised:
        validate_request({**REQUEST, "execution_mode": "yolo"})
    assert raised.value.code == "ROLLBACK_EXECUTION_MODE_INVALID"
