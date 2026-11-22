"""A rollback restores data, keeps the old one, and refuses half a quorum."""

import json

import pytest
from agent_contracts.kubernetes_rollback import (
    KubernetesRollbackError,
    planned_steps,
    simulation_plan,
    validate_request,
)
from target_agent.kubernetes_rollback_exec import RollbackExecutor

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


def test_a_multi_control_plane_cluster_blocks_when_the_local_node_is_unknown(monkeypatch):
    # A stacked etcd is restored member by member, which needs to know which
    # member this agent owns; without that nothing is touched.
    monkeypatch.delenv("KUBERNETES_UPGRADE_LOCAL_NODE", raising=False)
    runner = RollbackRunner(nodes=_nodes("v1.34.1", "v1.34.1", "v1.34.1"))

    result = RollbackExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_PREFLIGHT_BLOCKED"
    assert result["rolled_back"] is False
    assert any(
        check["name"] == "local.node" and not check["ok"]
        for check in result["preflight"]["checks"]
    )
    assert not [call for call in runner.calls if call[:1] == ["sudo"]]


def test_a_missing_snapshot_blocks_before_restore():
    runner = RollbackRunner(fail_on=("snapshot status",))
    result = RollbackExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "ROLLBACK_PREFLIGHT_BLOCKED"
    assert result["rolled_back"] is False
    assert result["performed_steps"] == []
    assert any(
        check["name"] == "etcd.snapshot.readable" and not check["ok"]
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


def test_assessment_is_read_only_and_names_the_refusal():
    runner = RollbackRunner(nodes=_nodes("v1.34.1"))
    assessed = RollbackExecutor(runner).assess(REQUEST)

    assert assessed["control_planes"] == 1
    assert assessed["control_plane"] == "cp-0"
    assert assessed["etcd_pod"] == "etcd-cp-0"
    assert assessed["blocked"] is False
    assert assessed["refusal"] == ""
    assert [check["name"] for check in assessed["checks"]][:2] == [
        "cluster.control_plane.count",
        "node.ready/cp-0",
    ]
    assert not [call for call in runner.calls if call[:1] == ["sudo"]]
    assert not [call for call in runner.calls if "restore" in call]


def test_assessment_reports_three_control_planes_without_touching_them(monkeypatch):
    monkeypatch.delenv("KUBERNETES_UPGRADE_LOCAL_NODE", raising=False)
    runner = RollbackRunner(nodes=_nodes("v1.34.1", "v1.34.1", "v1.34.1"))
    assessed = RollbackExecutor(runner).assess(REQUEST)

    assert assessed["control_planes"] == 3
    assert assessed["refusal"] == ""
    assert assessed["blocked"] is True
    assert [check["name"] for check in assessed["checks"]] == [
        "cluster.control_plane.count",
        "local.node",
    ]
    assert not [call for call in runner.calls if call[:1] == ["sudo"]]


def test_a_step_never_lists_the_same_command_twice():
    runner = RollbackRunner()
    result = RollbackExecutor(runner, api_wait_attempts=1).run(REQUEST)

    for step in result["steps"]:
        assert len(step["commands"]) == len(set(step["commands"]))
    assert "sudo systemctl start kubelet" in result["steps"][3]["commands"]
    assert "sudo systemctl stop kubelet" in result["steps"][2]["commands"]

def _multi_nodes():
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": f"cp-{index}",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "addresses": [{"type": "InternalIP", "address": f"10.0.0.1{index}"}],
                        "nodeInfo": {"kubeletVersion": "v1.34.1"},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
                for index in range(3)
            ]
        }
    )


def _multi_pods():
    return json.dumps(
        {
            "items": [
                {"metadata": {"name": f"etcd-cp-{index}"}, "spec": {"nodeName": f"cp-{index}"}}
                for index in range(3)
            ]
        }
    )


def _multi_members():
    return json.dumps(
        {
            "members": [
                {
                    "name": f"cp-{index}",
                    "peerURLs": [f"https://10.0.0.1{index}:2380"],
                }
                for index in range(3)
            ]
        }
    )


class MultiRollbackRunner:
    """Records every call and answers the way a three-member cluster would."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=20, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if argv[:2] == ["kubectl", "get"] and "nodes" in argv:
            return {"available": True, "returncode": 0, "stdout": _multi_nodes(), "stderr": ""}
        if "component=etcd" in joined:
            return {"available": True, "returncode": 0, "stdout": _multi_pods(), "stderr": ""}
        if "member" in argv and "list" in argv:
            return {"available": True, "returncode": 0, "stdout": _multi_members(), "stderr": ""}
        if "snapshot" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": '{"hash":"abc","revision":42}',
                "stderr": "",
            }
        if "health" in argv:
            return {"available": True, "returncode": 0, "stdout": "healthy", "stderr": ""}
        return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}


def test_the_snapshot_verdict_is_taken_where_the_file_lives(monkeypatch):
    """A remote pod cannot read a file that only exists on this host."""

    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = MultiRollbackRunner()
    assessed = RollbackExecutor(runner).assess(REQUEST)

    probes = [
        call for call in runner.calls if "snapshot" in call and "status" in call
    ]
    assert len(probes) == 1
    assert "etcd-cp-0" in probes[0]
    readable = {
        check["name"]: check
        for check in assessed["checks"]
        if check["name"].startswith("etcd.snapshot.readable")
    }
    assert set(readable) == {
        "etcd.snapshot.readable/cp-0",
        "etcd.snapshot.readable/cp-1",
        "etcd.snapshot.readable/cp-2",
    }
    assert all(check["ok"] for check in readable.values())
    assert "staged on cp-1" in readable["etcd.snapshot.readable/cp-1"]["detail"]


def test_a_three_member_etcd_is_restored_member_by_member(monkeypatch):
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    # Remote members are reached over ssh, so the executor's network hops are
    # explicit: staging the snapshot, the restore, both moves, the kubelet.
    monkeypatch.setenv("REMOTE_USER", "operator")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = MultiRollbackRunner()

    result = RollbackExecutor(runner, api_wait_attempts=1).run(REQUEST)

    assert result["failure_code"] is None
    assert result["rolled_back"] is True
    assert result["performed_steps"] == [
        "preflight",
        "etcd_restore",
        "data_swap",
        "kubelet_restart",
        "postflight",
    ]
    # The local member contributes the restore and its existence check; each
    # remote member also carries the copy that puts the snapshot on its own
    # etcd data path (mkdir, scp, sudo install, cleanup, readability probe).
    assert len(result["steps"][1]["commands"]) == 16
    # Every restore names the whole cluster and its own peer URL, so the three
    # members come up as one cluster instead of three unrelated clusters.
    restores = [call for call in runner.calls if "restore" in " ".join(call)]
    assert len(restores) == 3
    assert all("--initial-cluster=" in " ".join(call) for call in restores)
    # Every restore runs locally through kubectl exec against that member's pod;
    # the remote hosts only receive the snapshot and the directory swap.
    assert all(call[:1] == ["kubectl"] for call in restores)
    assert all(f"etcd-cp-{index}" in call for index, call in enumerate(restores))
    scp = [call for call in runner.calls if call[:1] == ["scp"]]
    assert len(scp) == 2
    assert all(REQUEST["snapshot_path"] in " ".join(call) for call in scp)
    # scp writes as the login user, so the bytes land in /tmp first and reach
    # the root-owned etcd path through sudo install.
    assert all(call[-1].endswith(":/tmp/snapshot-pre-v1.34.1.db") for call in scp)
    installs = [call for call in runner.calls if "install" in " ".join(call)]
    assert len(installs) == 2
    installed = "sudo install -m 0444 /tmp/snapshot-pre-v1.34.1.db"
    assert all(installed in " ".join(call) for call in installs)
    # The temporary copy is removed again on every remote host.
    cleanups = [
        call for call in runner.calls if "rm" in call and "-f" in call
    ]
    assert len(cleanups) == 2
    assert all("/tmp/snapshot-pre-v1.34.1.db" in " ".join(call) for call in cleanups)
    # Every kubelet stops before the first directory moves.
    stops = [
        index
        for index, call in enumerate(runner.calls)
        if "systemctl" in " ".join(call) and "stop" in " ".join(call)
    ]
    swaps = [index for index, call in enumerate(runner.calls) if "mv" in " ".join(call)]
    assert min(stops) < min(swaps)
    assert len(stops) == 3
    assert len([call for call in runner.calls if "stop kubelet" in " ".join(call)]) == 3
    starts = [
        index
        for index, call in enumerate(runner.calls)
        if "systemctl" in " ".join(call) and "start" in " ".join(call)
    ]
    assert len(starts) == 3
    # The local member starts first so the quorum forms in a known order.
    assert "cp-0" not in " ".join(runner.calls[starts[0]])


def _ipv6_nodes():
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": f"cp-{index}",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "addresses": [
                            {"type": "InternalIP", "address": f"fd00::1{index}"}
                        ],
                        "nodeInfo": {"kubeletVersion": "v1.34.1"},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
                for index in range(3)
            ]
        }
    )


def _ipv6_members():
    return json.dumps(
        {
            "members": [
                {"name": f"cp-{index}", "peerURLs": [f"https://[fd00::1{index}]:2380"]}
                for index in range(3)
            ]
        }
    )


class Ipv6RollbackRunner(MultiRollbackRunner):
    def __call__(self, argv, timeout=20, **kwargs):
        if argv[:2] == ["kubectl", "get"] and "nodes" in argv:
            self.calls.append(list(argv))
            return {"available": True, "returncode": 0, "stdout": _ipv6_nodes(), "stderr": ""}
        if "member" in argv and "list" in argv:
            self.calls.append(list(argv))
            return {"available": True, "returncode": 0, "stdout": _ipv6_members(), "stderr": ""}
        return super().__call__(argv, timeout, **kwargs)


def test_a_bracketed_ipv6_peer_url_still_matches_its_member(monkeypatch):
    """``https://[fd00::1]:2380`` must resolve to the node's ``fd00::1``."""

    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = Ipv6RollbackRunner()
    assessed = RollbackExecutor(runner).assess(REQUEST)

    assert assessed["blocked"] is False
    members = assessed["members"]
    assert [item["node"] for item in members] == ["cp-0", "cp-1", "cp-2"]
    assert [item["address"] for item in members] == ["fd00::10", "fd00::11", "fd00::12"]
    assert all(item["pod"] == f"etcd-{item['node']}" for item in members)


def test_a_snapshot_outside_the_etcd_pod_mount_blocks_before_any_change():
    """The etcd pod mounts /var/lib/etcd only; anywhere else cannot be read."""

    runner = RollbackRunner()
    request = dict(REQUEST)
    request["snapshot_path"] = "/var/backups/etcd/snapshot-pre-v1.34.1.db"
    assessed = RollbackExecutor(runner).assess(request)

    assert assessed["blocked"] is True
    path_check = next(
        check for check in assessed["checks"] if check["name"] == "rollback.snapshot.path"
    )
    assert path_check["ok"] is False
    assert "/var/lib/etcd" in path_check["detail"]
    # Nothing probed the pod for a file it cannot see.
    assert not [call for call in runner.calls if "snapshot" in call and "status" in call]


def test_a_snapshot_inside_the_mount_is_probed_once(monkeypatch):
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = MultiRollbackRunner()
    assessed = RollbackExecutor(runner).assess(REQUEST)

    path_check = next(
        check for check in assessed["checks"] if check["name"] == "rollback.snapshot.path"
    )
    assert path_check["ok"] is True
    assert assessed["blocked"] is False


class NoPasswordSudoRunner(MultiRollbackRunner):
    """A lab where ssh works but remote sudo still asks for a password."""

    def __call__(self, argv, timeout=20, **kwargs):
        if argv[:1] == ["ssh"] and "sudo" in argv and "-n" in argv:
            self.calls.append(list(argv))
            return {
                "available": True,
                "returncode": 1,
                "stdout": "",
                "stderr": "sudo: a password is required\n",
            }
        return super().__call__(argv, timeout, **kwargs)


def test_remote_sudo_that_needs_a_password_blocks_the_rollback(monkeypatch):
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = NoPasswordSudoRunner()
    assessed = RollbackExecutor(runner).assess(REQUEST)

    assert assessed["blocked"] is True
    sudo_checks = [
        check for check in assessed["checks"] if check["name"].startswith("ssh.sudo/")
    ]
    assert [check["name"] for check in sudo_checks] == ["ssh.sudo/cp-1", "ssh.sudo/cp-2"]
    assert all(check["ok"] is False for check in sudo_checks)
    assert "NOPASSWD" in sudo_checks[0]["detail"]
    # Nothing was stopped or moved: the refusal happened in preflight.
    assert not [call for call in runner.calls if "systemctl" in " ".join(call)]


def test_the_local_member_needs_no_ssh_at_all(monkeypatch):
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    monkeypatch.setenv("KUBERNETES_UPGRADE_REMOTE_USER", "operator")
    runner = MultiRollbackRunner()
    RollbackExecutor(runner).assess(REQUEST)

    reachable = [
        call for call in runner.calls
        if call[:1] == ["ssh"] and "sudo" not in call
    ]
    hosts = [call[-2] for call in reachable]
    assert hosts == ["operator@10.0.0.11", "operator@10.0.0.12"]
