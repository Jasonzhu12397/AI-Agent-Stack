"""A real upgrade run must refuse what it cannot do and report what it did."""

import json

import pytest
from target_agent.kubernetes_upgrade_exec import (
    PACKAGE_MANAGER_ENV,
    PACKAGE_VERSION_ENV,
    REMOTE_USER_ENV,
    UpgradeExecutionError,
    UpgradeExecutor,
)

REQUEST = {
    "cluster_name": "edge-lab",
    "current_version": "v1.33.4",
    "target_version": "v1.34.1",
    "execution_mode": "execute",
}


def _nodes(*entries):
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": name, "labels": labels},
                    "status": {
                        "addresses": [{"type": "InternalIP", "address": address}],
                        "nodeInfo": {"kubeletVersion": version},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
                for name, labels, address, version in entries
            ]
        }
    )


CONTROL = ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.33.4")


class FakeRunner:
    """Answers the preflight reads the way a healthy single-node cluster would."""

    def _answer(self, argv):
        joined = " ".join(argv)
        if "configmap" in argv:
            return json.dumps(
                {
                    "data": {
                        "ClusterConfiguration": "kubernetesVersion: v1.33.4\n"
                    }
                }
            )
        if "poddisruptionbudgets" in joined:
            return json.dumps({"items": []})
        if "component=etcd" in joined:
            return json.dumps({"items": []})
        if "status" in argv and "snapshot" in argv:
            return '{"hash":"sha256:abc","revision":42}'
        return self._nodes

    """A cluster that reports the new kubelet version once kubeadm applied it."""

    def __init__(
        self,
        *,
        nodes,
        fail_on=(),
        upgraded=None,
        peer_upgraded=None,
        fail_when=None,
        db_size=131072,
        free_bytes=20 * 1024**3,
        free_stdout=None,
        status_stdout=None,
    ):
        self.calls = []
        self._nodes = nodes
        self._upgraded = upgraded or nodes
        self._peer_upgraded = dict(peer_upgraded or {})
        self._fail_on = tuple(fail_on)
        self._fail_when = fail_when
        self._db_size = db_size
        self._free_bytes = free_bytes
        self._free_stdout = free_stdout
        self._status_stdout = status_stdout

    def __call__(self, argv, timeout=20, **kwargs):
        self.calls.append(list(argv))
        if "endpoint" in argv and "status" in argv:
            # The upgrade reads the live database size before it writes a
            # snapshot: a snapshot that does not fit is not a backup.
            if self._status_stdout is not None:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": self._status_stdout,
                    "stderr": "",
                }
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"Header": {"dbSize": self._db_size}}]),
                "stderr": "",
            }
        if argv[:1] == ["sudo"] and "df" in argv:
            if self._free_stdout is not None:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": self._free_stdout,
                    "stderr": "",
                }
            return {
                "available": True,
                "returncode": 0,
                "stdout": (
                    "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                    f"/dev/sda1 1000000000 1 {self._free_bytes} 1% /var/lib/etcd\n"
                ),
                "stderr": "",
            }
        if "snapshot" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": '{"hash":"sha256:abc","revision":42}',
                "stderr": "",
            }
        if argv and argv[0] == "kubectl" and "get" in argv:
            return {"available": True, "returncode": 0, "stdout": self._answer(argv), "stderr": ""}
        if self._fail_when is not None and self._fail_when(argv):
            return {"available": True, "returncode": 255, "stdout": "", "stderr": "ssh: refused"}
        for marker in self._fail_on:
            if marker in argv:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "boom"}
        if argv[:3] == ["kubeadm", "upgrade", "apply"]:
            self._nodes = self._upgraded
        if argv[:1] == ["ssh"] and "kubeadm" in argv and "node" in argv:
            address = argv[1].split("@")[-1]
            if address in self._peer_upgraded:
                self._nodes = self._peer_upgraded[address]
        return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}


UPGRADED_CONTROL = ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.34.1")


def test_single_node_cluster_is_upgraded():
    runner = FakeRunner(nodes=_nodes(CONTROL), upgraded=_nodes(UPGRADED_CONTROL))
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is None
    assert result["upgrade_performed"] is True
    assert result["upgrade_simulated"] is False
    assert [step["status"] for step in result["upgrade_steps"]] == [
        "succeeded",
        "succeeded",
        "skipped",
        "succeeded",
    ]
    assert ["kubeadm", "upgrade", "apply", "-y", "v1.34.1"] in runner.calls


def test_worker_upgrade_uses_the_configured_remote_user(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    nodes = _nodes(CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4"))
    runner = FakeRunner(
        nodes=_nodes(CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4")),
        upgraded=_nodes(UPGRADED_CONTROL, ("w-0", {}, "10.0.0.20", "v1.34.1")),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert nodes  # the cluster really had a worker
    assert result["failure_code"] is None
    assert ["ssh", "metal@10.0.0.20", "kubeadm", "upgrade", "node"] in runner.calls
    assert result["upgrade_steps"][2]["status"] == "succeeded"


def test_workers_without_a_remote_executor_are_refused(monkeypatch):
    monkeypatch.delenv(REMOTE_USER_ENV, raising=False)
    runner = FakeRunner(nodes=_nodes(CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4")))

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_REMOTE_EXECUTOR_NOT_CONFIGURED"
    # Only the node read happened: no drain, no kubeadm, nothing was changed.
    assert runner.calls == [["kubectl", "get", "nodes", "-o", "json"]]


SECOND_CONTROL = (
    "cp-1",
    {"node-role.kubernetes.io/control-plane": ""},
    "10.0.0.11",
    "v1.33.4",
)


def test_a_rolling_control_plane_upgrade_runs_upgrade_node_on_every_peer(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    target_nodes = _nodes(
        UPGRADED_CONTROL,
        ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.11", "v1.34.1"),
        ("w-0", {}, "10.0.0.20", "v1.34.1"),
    )
    runner = FakeRunner(
        nodes=_nodes(CONTROL, SECOND_CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4")),
        upgraded=_nodes(
            UPGRADED_CONTROL,
            SECOND_CONTROL,
            ("w-0", {}, "10.0.0.20", "v1.33.4"),
        ),
        peer_upgraded={
            "10.0.0.11": _nodes(
                UPGRADED_CONTROL,
                ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.11", "v1.34.1"),
                ("w-0", {}, "10.0.0.20", "v1.33.4"),
            ),
            "10.0.0.20": target_nodes,
        },
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is None
    # kubeadm prescribes exactly one `kubeadm upgrade apply`, on the local control plane.
    assert [call for call in runner.calls if call[:3] == ["kubeadm", "upgrade", "apply"]] == [
        ["kubeadm", "upgrade", "apply", "-y", "v1.34.1"]
    ]
    assert ["ssh", "metal@10.0.0.11", "kubeadm", "upgrade", "node"] in runner.calls
    assert ["ssh", "metal@10.0.0.11", "sudo", "systemctl", "restart", "kubelet"] in runner.calls
    assert ["ssh", "metal@10.0.0.20", "kubeadm", "upgrade", "node"] in runner.calls
    assert [step["status"] for step in result["upgrade_steps"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert result["upgrade_performed"] is True
    assert "control_plane:cp-1" in result["performed_steps"]


def test_an_unidentified_local_control_plane_is_refused(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.delenv("KUBERNETES_UPGRADE_LOCAL_NODE", raising=False)
    monkeypatch.setattr(
        "target_agent.kubernetes_upgrade_exec.socket.gethostname", lambda: "build-box"
    )
    runner = FakeRunner(nodes=_nodes(CONTROL, SECOND_CONTROL))

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_LOCAL_NODE_UNKNOWN"
    assert "KUBERNETES_UPGRADE_LOCAL_NODE" in excinfo.value.detail
    assert runner.calls == [["kubectl", "get", "nodes", "-o", "json"]]


def test_a_control_plane_peer_without_an_address_is_refused(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    runner = FakeRunner(
        nodes=_nodes(
            CONTROL,
            ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "", "v1.33.4"),
        ),
    )

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_CONTROL_PLANE_PEER_ADDRESS_UNKNOWN"


def test_a_peer_upgrade_failure_names_the_peer(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    runner = FakeRunner(
        nodes=_nodes(CONTROL, SECOND_CONTROL),
        fail_when=lambda argv: "kubeadm" in argv
        and "node" in argv
        and "10.0.0.11" in " ".join(argv),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_CONTROL_PLANE_PEER_FAILED"
    assert result["upgrade_steps"][1]["status"] == "failed"
    assert result["upgrade_steps"][1]["detail"].startswith("cp-1:")


def test_a_control_plane_peer_already_on_the_target_is_skipped(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    runner = FakeRunner(
        nodes=_nodes(
            CONTROL,
            ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.11", "v1.34.1"),
        ),
        upgraded=_nodes(
            UPGRADED_CONTROL,
            ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.11", "v1.34.1"),
        ),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is None
    assert not [call for call in runner.calls if call[:1] == ["ssh"]]
    assert "cp-1: already on v1.34.1, skipped" in result["upgrade_steps"][1]["detail"]


def test_a_failed_preflight_stops_the_run():
    runner = FakeRunner(nodes=_nodes(CONTROL), fail_on=["plan"])
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_FAILED"
    assert result["upgrade_performed"] is False
    assert result["upgrade_steps"][0]["status"] == "failed"
    assert result["upgrade_steps"][1]["status"] == "not_started"


def test_postflight_rejects_nodes_still_on_the_old_minor():
    old = _nodes(("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.33.4"))
    runner = FakeRunner(nodes=old)
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_POSTFLIGHT_FAILED"
    assert "v1.33" in result["upgrade_steps"][3]["detail"]


def test_unreadable_node_list_refuses_to_start():
    runner = FakeRunner(nodes="")
    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)
    assert excinfo.value.code == "UPGRADE_NODES_JSON_INVALID"


def test_package_upgrade_runs_before_kubeadm_apply(monkeypatch):
    monkeypatch.setenv(PACKAGE_MANAGER_ENV, "apt")
    monkeypatch.setenv(PACKAGE_VERSION_ENV, "1.34.1-1.1")
    runner = FakeRunner(nodes=_nodes(CONTROL), upgraded=_nodes(UPGRADED_CONTROL))
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["package_upgrade"] == "configured"
    assert result["failure_code"] is None
    package = [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "--allow-change-held-packages",
        "kubelet=1.34.1-1.1",
        "kubeadm=1.34.1-1.1",
        "kubectl=1.34.1-1.1",
    ]
    assert package in runner.calls
    assert runner.calls.index(package) < runner.calls.index(
        ["kubeadm", "upgrade", "apply", "-y", "v1.34.1"]
    )


def test_package_upgrade_reaches_the_worker_through_ssh(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv(PACKAGE_MANAGER_ENV, "dnf")
    monkeypatch.setenv(PACKAGE_VERSION_ENV, "1.34.1-1500")
    runner = FakeRunner(
        nodes=_nodes(CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4")),
        upgraded=_nodes(UPGRADED_CONTROL, ("w-0", {}, "10.0.0.20", "v1.34.1")),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is None
    remote_package = [
        "ssh",
        "metal@10.0.0.20",
        "sudo dnf install -y kubelet-1.34.1-1500 kubeadm-1.34.1-1500 kubectl-1.34.1-1500",
    ]
    assert remote_package in runner.calls
    assert result["upgrade_steps"][2]["status"] == "succeeded"


def test_a_package_version_from_another_release_is_refused(monkeypatch):
    monkeypatch.setenv(PACKAGE_MANAGER_ENV, "apt")
    monkeypatch.setenv(PACKAGE_VERSION_ENV, "1.33.4-1.1")
    runner = FakeRunner(nodes=_nodes(CONTROL))

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_PACKAGE_VERSION_MISMATCH"
    assert runner.calls == [["kubectl", "get", "nodes", "-o", "json"]]


def test_an_unknown_package_manager_is_refused(monkeypatch):
    monkeypatch.setenv(PACKAGE_MANAGER_ENV, "yum")
    runner = FakeRunner(nodes=_nodes(CONTROL))

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_PACKAGE_MANAGER_UNSUPPORTED"


def test_a_failed_package_install_stops_the_run(monkeypatch):
    monkeypatch.setenv(PACKAGE_MANAGER_ENV, "apt")
    monkeypatch.setenv(PACKAGE_VERSION_ENV, "1.34.1-1.1")
    runner = FakeRunner(nodes=_nodes(CONTROL), fail_on=["apt-get"])
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PACKAGE_FAILED"
    assert result["upgrade_performed"] is False
    assert runner.calls[-1][0] == "sudo"


def _nodes_with(node_overrides, *, config_version="v1.33.4", pdb=None, etcd=False):
    """Build raw kubectl answers so a single preflight fact can be made wrong."""

    def runner(argv, timeout=20, **kwargs):
        runner.calls.append(list(argv))
        joined = " ".join(argv)
        if argv and argv[0] == "kubectl" and "get" in argv:
            if "configmap" in argv:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "data": {
                                "ClusterConfiguration": f"kubernetesVersion: {config_version}\n"
                            }
                        }
                    ),
                    "stderr": "",
                }
            if "poddisruptionbudgets" in joined:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps({"items": pdb or []}),
                    "stderr": "",
                }
            if "component=etcd" in joined:
                items = [{"metadata": {"name": "etcd-cp-0"}}] if etcd else []
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps({"items": items}),
                    "stderr": "",
                }
            return {
                "available": True,
                "returncode": 0,
                "stdout": node_overrides,
                "stderr": "",
            }
        if argv[:3] == ["kubeadm", "upgrade", "apply"]:
            return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}
        if "endpoint" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"Header": {"dbSize": 131072}}]),
                "stderr": "",
            }
        if argv[:1] == ["sudo"] and "df" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": (
                    "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                    "/dev/sda1 1000000000 1 21474836480 1% /var/lib/etcd\n"
                ),
                "stderr": "",
            }
        if "snapshot" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": '{"hash":"sha256:abc","revision":42}',
                "stderr": "",
            }
        return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    runner.calls = []
    return runner


def _raw_nodes(name="cp-0", ready="True", unschedulable=False, version="v1.33.4"):
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": name,
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "spec": {"unschedulable": unschedulable},
                    "status": {
                        "addresses": [{"type": "InternalIP", "address": "10.0.0.10"}],
                        "nodeInfo": {"kubeletVersion": version},
                        "conditions": [{"type": "Ready", "status": ready}],
                    },
                }
            ]
        }
    )


def test_a_not_ready_node_blocks_the_preflight():
    runner = _nodes_with(_raw_nodes(ready="False"))
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "node.ready/cp-0" in failed
    assert not any(call[:3] == ["kubeadm", "upgrade", "plan"] for call in runner.calls)


def test_a_cordoned_node_blocks_the_preflight():
    runner = _nodes_with(_raw_nodes(unschedulable=True))
    result = UpgradeExecutor(runner).run(REQUEST)

    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "node.schedulable/cp-0" in failed


def test_a_kubeadm_config_on_another_minor_blocks_the_preflight():
    runner = _nodes_with(_raw_nodes(), config_version="v1.32.4")
    result = UpgradeExecutor(runner).run(REQUEST)

    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "kubeadm-config.version" in failed


def test_stacked_etcd_is_backed_up_and_verified_before_the_upgrade():
    runner = _nodes_with(_raw_nodes(), etcd=True)
    result = UpgradeExecutor(runner).run(REQUEST)

    snapshot = result["etcd_snapshot"]
    assert snapshot["taken"] is True
    assert snapshot["verified"] is True
    # The cluster name is part of the file name so the recurring backup job can
    # attribute this file to this cluster and prune it when it is no longer needed.
    assert snapshot["path"].startswith(
        "/var/lib/etcd/backups/snapshot-pre-edge-lab-v1.34.1-"
    )
    assert ["sudo", "mkdir", "-p", "/var/lib/etcd/backups"] in runner.calls
    saved = [call for call in runner.calls if "snapshot" in call and "save" in call]
    checked = [call for call in runner.calls if "snapshot" in call and "status" in call]
    # The file is staged as *.partial, read back there, and only renamed once it
    # verifies: nothing unreadable ever carries a snapshot's real name.
    assert saved and saved[0][-1] == snapshot["path"] + ".partial"
    assert checked and checked[0][-1] == snapshot["path"] + ".partial"
    assert ["sudo", "mv", snapshot["path"] + ".partial", snapshot["path"]] in runner.calls
    etcd_check = next(
        check for check in result["preflight"]["checks"] if check["name"] == "etcd.snapshot"
    )
    assert etcd_check["ok"] is True


def test_a_snapshot_that_cannot_be_saved_blocks_the_upgrade():
    base = _nodes_with(_raw_nodes(), etcd=True)

    def runner(argv, timeout=20, **kwargs):
        if "snapshot" in argv and "save" in argv:
            return {"available": True, "returncode": 1, "stdout": "", "stderr": "no auth"}
        return base(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "no auth" in failed["etcd.snapshot"]["detail"]
    assert result["etcd_snapshot"] is None


def test_a_snapshot_that_cannot_be_read_back_blocks_the_upgrade():
    base = _nodes_with(_raw_nodes(), etcd=True)
    calls = []

    def runner(argv, timeout=20, **kwargs):
        calls.append(list(argv))
        if "snapshot" in argv and "status" in argv:
            return {"available": True, "returncode": 1, "stdout": "", "stderr": "bad file"}
        return base(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "bad file" in failed["etcd.snapshot"]["detail"]
    removed = [call for call in calls if call[:2] == ["sudo", "rm"]]
    assert removed and removed[0][-1].endswith(".db.partial")


def test_an_unreadable_etcd_list_blocks_the_upgrade():
    base = _nodes_with(_raw_nodes(), etcd=True)

    def runner(argv, timeout=20, **kwargs):
        if "component=etcd" in " ".join(argv):
            return {"available": True, "returncode": 1, "stdout": "", "stderr": "forbidden"}
        return base(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "UPGRADE_ETCD_UNREADABLE" in failed["etcd.snapshot"]["detail"]


def test_a_supplied_snapshot_is_reused_instead_of_taking_a_new_one(monkeypatch, tmp_path):
    # The file must live where the etcd pod can read it; the lab's mount is
    # /var/lib/etcd, so the test points the rule at its own directory.
    from packages.agent_contracts import kubernetes_rollback

    monkeypatch.setattr(kubernetes_rollback, "DEFAULT_ETCD_DATA_DIR", str(tmp_path))
    supplied = tmp_path / "backups" / "etcd.db"
    supplied.parent.mkdir(parents=True, exist_ok=True)
    supplied.write_bytes(b"snapshot")
    monkeypatch.setenv("KUBERNETES_UPGRADE_ETCD_SNAPSHOT", str(supplied))
    runner = _nodes_with(_raw_nodes(), etcd=True)
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["etcd_snapshot"] == {"path": str(supplied), "verified": True, "taken": False}
    assert not [call for call in runner.calls if "snapshot restore" in " ".join(call)]
    assert any("status" in call for call in runner.calls)

    import os
    import time

    old = time.time() - 72 * 3600
    os.utime(supplied, (old, old))
    monkeypatch.setenv("KUBERNETES_UPGRADE_ETCD_SNAPSHOT_MAX_AGE_HOURS", "24")
    stale = UpgradeExecutor(_nodes_with(_raw_nodes(), etcd=True)).run(REQUEST)
    assert stale["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"] for check in stale["preflight"]["checks"] if not check["ok"]}
    assert "etcd.snapshot" in failed


def test_a_supplied_snapshot_that_does_not_verify_blocks_the_upgrade(monkeypatch, tmp_path):
    from packages.agent_contracts import kubernetes_rollback

    monkeypatch.setattr(kubernetes_rollback, "DEFAULT_ETCD_DATA_DIR", str(tmp_path))
    supplied = tmp_path / "backups" / "etcd.db"
    supplied.parent.mkdir(parents=True, exist_ok=True)
    supplied.write_bytes(b"not a snapshot")
    monkeypatch.setenv("KUBERNETES_UPGRADE_ETCD_SNAPSHOT", str(supplied))
    base = _nodes_with(_raw_nodes(), etcd=True)

    def runner(argv, timeout=20, **kwargs):
        if "snapshot" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 1,
                "stdout": "",
                "stderr": "snapshot file integrity error",
            }
        return base(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "integrity error" in failed["etcd.snapshot"]["detail"]
    assert result["etcd_snapshot"] is None


def test_a_pod_disruption_budget_at_zero_blocks_the_preflight():
    runner = _nodes_with(
        _raw_nodes(),
        pdb=[{"metadata": {"name": "database"}, "status": {"disruptionsAllowed": 0}}],
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    failed = {check["name"] for check in result["preflight"]["checks"] if not check["ok"]}
    assert "poddisruptionbudget.headroom" in failed


def test_an_unreachable_peer_blocks_the_run_before_the_local_control_plane(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    runner = FakeRunner(
        nodes=_nodes(CONTROL, SECOND_CONTROL),
        fail_when=lambda argv: argv[:1] == ["ssh"]
        and argv[-1] == "true"
        and "10.0.0.11" in " ".join(argv),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "ssh.reachable/cp-1" in failed
    assert result["upgrade_performed"] is False
    assert not [call for call in runner.calls if call[:3] == ["kubeadm", "upgrade", "apply"]]


def test_every_remote_node_gets_an_ssh_reachability_check(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    runner = FakeRunner(
        nodes=_nodes(CONTROL, ("w-0", {}, "10.0.0.20", "v1.33.4")),
        upgraded=_nodes(UPGRADED_CONTROL, ("w-0", {}, "10.0.0.20", "v1.34.1")),
    )
    result = UpgradeExecutor(runner).run(REQUEST)

    names = {check["name"] for check in result["preflight"]["checks"] if check["ok"]}
    assert {"ssh.reachable/w-0", "node.ready/cp-0"} <= names
    assert result["failure_code"] is None


def _probe_counter(base, failing):
    """Fail the given 1-based etcd health probes, pass the rest."""

    seen = {"count": 0}

    def runner(argv, timeout=20, **kwargs):
        if "endpoint" in argv and "health" in argv:
            seen["count"] += 1
            if seen["count"] in failing:
                return {
                    "available": True,
                    "returncode": 2,
                    "stdout": "",
                    "stderr": "context deadline exceeded",
                }
        return base(argv, timeout=timeout, **kwargs)

    return runner


def test_losing_etcd_health_after_the_local_apply_stops_before_any_peer(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    base = _nodes_with(_nodes(CONTROL, SECOND_CONTROL), etcd=True)
    runner = _probe_counter(base, {2})
    result = UpgradeExecutor(
        runner, etcd_health_attempts=1, etcd_health_interval=0
    ).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_ETCD_QUORUM_AT_RISK"
    assert "cp-0" in result["upgrade_steps"][1]["detail"]
    # Only the reachability probe is allowed to run; no peer is upgraded.
    assert not [call for call in base.calls if "kubeadm" in call and call[:1] == ["ssh"]]
    assert result["etcd_health"][-1]["name"] == "etcd.quorum/cp-0"


def test_losing_etcd_health_after_a_peer_stops_before_the_next_peer(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    nodes = _nodes(CONTROL, SECOND_CONTROL)
    base = _nodes_with(nodes, etcd=True)
    runner = _probe_counter(base, {3})
    result = UpgradeExecutor(
        runner, etcd_health_attempts=1, etcd_health_interval=0
    ).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_ETCD_QUORUM_AT_RISK"
    assert result["etcd_health"][-1]["name"] == "etcd.quorum/cp-1"


def test_a_transient_etcd_failure_is_waited_out(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    monkeypatch.setenv("KUBERNETES_UPGRADE_LOCAL_NODE", "cp-0")
    base = _nodes_with(_nodes(CONTROL, SECOND_CONTROL), etcd=True)
    runner = _probe_counter(base, {2})
    monkeypatch.setattr("target_agent.kubernetes_upgrade_exec.time.sleep", lambda _s: None)
    result = UpgradeExecutor(
        runner, etcd_health_attempts=2, etcd_health_interval=0
    ).run(REQUEST)

    assert result["failure_code"] is not None  # postflight still sees old versions
    assert result["etcd_health"][0]["ok"] is True
    assert "healthy after 2 probe(s)" in result["etcd_health"][0]["detail"]


def test_an_unhealthy_cluster_is_refused_before_anything_runs():
    base = _nodes_with(_nodes(CONTROL), etcd=True)
    runner = _probe_counter(base, {1})
    result = UpgradeExecutor(
        runner, etcd_health_attempts=1, etcd_health_interval=0
    ).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "etcd.quorum" in failed
    assert result["upgrade_performed"] is False


def test_workers_already_on_the_target_are_not_drained_and_upgraded_again(monkeypatch):
    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    nodes = _nodes(
        ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.34.1"),
        ("w-0", {}, "10.0.0.20", "v1.34.1"),
    )
    runner = FakeRunner(nodes=nodes)
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is None
    assert result["already_at_target"] is True
    assert result["upgrade_performed"] is False
    assert ["kubectl", "drain", "w-0", "--ignore-daemonsets", "--delete-emptydir-data",
            "--timeout=15m"] not in runner.calls
    # `kubeadm upgrade plan` is a read-only preflight step and still runs.
    assert not [
        call
        for call in runner.calls
        if call[:3] in (["kubeadm", "upgrade", "apply"], ["kubeadm", "upgrade", "node"])
    ]
    assert result["upgrade_steps"][1]["status"] == "skipped"
    assert "every control plane already runs v1.34.1" in result["upgrade_steps"][1]["detail"]
    assert result["upgrade_steps"][2]["status"] == "skipped"
    assert "every worker already on v1.34.1" in result["upgrade_steps"][2]["detail"]


def test_a_supplied_snapshot_outside_the_etcd_mount_blocks_with_the_fix_named(
    monkeypatch, tmp_path,
):
    supplied = tmp_path / "elsewhere" / "etcd.db"
    supplied.parent.mkdir(parents=True, exist_ok=True)
    supplied.write_bytes(b"snapshot")
    monkeypatch.setenv("KUBERNETES_UPGRADE_ETCD_SNAPSHOT", str(supplied))
    runner = _nodes_with(_raw_nodes(), etcd=True)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] == "UPGRADE_PREFLIGHT_BLOCKED"
    failed = {check["name"]: check for check in result["preflight"]["checks"] if not check["ok"]}
    assert "/var/lib/etcd" in failed["etcd.snapshot"]["detail"]
    assert "move the snapshot under /var/lib/etcd/backups" in failed["etcd.snapshot"]["detail"]
    # No pod probe was attempted for a file the pod cannot see.
    assert not [
        call for call in runner.calls if "snapshot" in call and "status" in call
    ]


def test_a_node_on_a_lower_patch_is_not_treated_as_done(monkeypatch):
    """v1.34.0 must not satisfy a request for v1.34.3."""

    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    nodes = _nodes(
        ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.34.0"),
        ("w-0", {}, "10.0.0.20", "v1.34.0"),
    )
    runner = FakeRunner(nodes=nodes)
    request = dict(REQUEST)
    result = UpgradeExecutor(runner).run(request)

    assert result["already_at_target"] is False
    assert [
        call for call in runner.calls if call[:3] == ["kubeadm", "upgrade", "apply"]
    ] == [["kubeadm", "upgrade", "apply", "-y", request["target_version"]]]
    assert ["kubectl", "drain", "w-0", "--ignore-daemonsets", "--delete-emptydir-data",
            "--timeout=15m"] in runner.calls


def test_a_node_above_the_target_is_skipped(monkeypatch):
    """A host already past the requested patch keeps running what it has."""

    monkeypatch.setenv(REMOTE_USER_ENV, "metal")
    nodes = _nodes(
        ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.34.9"),
        ("w-0", {}, "10.0.0.20", "v1.34.9"),
    )
    runner = FakeRunner(nodes=nodes)
    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["already_at_target"] is True
    assert result["upgrade_performed"] is False
    # `kubeadm upgrade plan` is a read-only preflight and still runs.
    assert not [
        call
        for call in runner.calls
        if call[:3] in (["kubeadm", "upgrade", "apply"], ["kubeadm", "upgrade", "node"])
    ]


def test_a_snapshot_that_does_not_fit_the_volume_blocks_the_upgrade():
    # A 5 GiB database on a volume with 5 GiB free: the snapshot plus the
    # 512 MiB floor does not fit, so writing it would fill the volume the live
    # cluster is still writing to.
    healthy = _nodes_with(_raw_nodes(), etcd=True)
    calls = []

    def runner(argv, timeout=20, **kwargs):
        if "endpoint" in argv and "status" in argv:
            calls.append(list(argv))
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"Header": {"dbSize": 5 * 1024**3}}]),
                "stderr": "",
            }
        if argv[:1] == ["sudo"] and "df" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": (
                    "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                    f"/dev/sda1 1000000000 1 {5 * 1024**3} 1% /var/lib/etcd\n"
                ),
                "stderr": "",
            }
        if "snapshot" in argv and "save" in argv:
            calls.append(list(argv))
        return healthy(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["failure_code"] is not None
    assert result["etcd_snapshot"] is None
    assert not [call for call in calls if "save" in call]
    snapshot_check = next(
        check
        for check in result["preflight"]["checks"]
        if check["name"] == "etcd.snapshot"
    )
    assert "ETCD_BACKUP_DISK_LOW" in snapshot_check["detail"]


def test_a_volume_that_cannot_be_measured_refuses_to_take_a_snapshot():
    healthy = _nodes_with(_raw_nodes(), etcd=True)

    def runner(argv, timeout=20, **kwargs):
        if "endpoint" in argv and "status" in argv:
            return {"available": True, "returncode": 0, "stdout": "not json", "stderr": ""}
        return healthy(argv, timeout=timeout, **kwargs)

    result = UpgradeExecutor(runner).run(REQUEST)

    assert result["etcd_snapshot"] is None
    snapshot_check = next(
        check
        for check in result["preflight"]["checks"]
        if check["name"] == "etcd.snapshot"
    )
    assert "ETCD_BACKUP_DISK_CHECK_FAILED" in snapshot_check["detail"]


def test_the_snapshot_is_written_where_the_request_says_retention_looks():
    runner = _nodes_with(_raw_nodes(), etcd=True)
    request = {**REQUEST, "snapshot_directory": "/var/lib/etcd/backups-labcache"}

    result = UpgradeExecutor(runner).run(request)

    assert result["etcd_snapshot"]["path"].startswith(
        "/var/lib/etcd/backups-labcache/snapshot-pre-edge-lab-v1.34.1-"
    )
