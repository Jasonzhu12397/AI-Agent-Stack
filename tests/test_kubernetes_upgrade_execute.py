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
                    },
                }
                for name, labels, address, version in entries
            ]
        }
    )


CONTROL = ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.33.4")


class FakeRunner:
    """A cluster that reports the new kubelet version once kubeadm applied it."""

    def __init__(self, *, nodes, fail_on=(), upgraded=None):
        self.calls = []
        self._nodes = nodes
        self._upgraded = upgraded or nodes
        self._fail_on = tuple(fail_on)

    def __call__(self, argv, timeout=20, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["kubectl", "get"]:
            nodes = self._nodes if argv[:3] == ["kubectl", "get", "nodes"] else self._nodes
            return {"available": True, "returncode": 0, "stdout": nodes, "stderr": ""}
        for marker in self._fail_on:
            if marker in argv:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "boom"}
        if argv[:3] == ["kubeadm", "upgrade", "apply"]:
            self._nodes = self._upgraded
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


def test_multiple_control_planes_are_refused():
    runner = FakeRunner(
        nodes=_nodes(
            CONTROL,
            ("cp-1", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.11", "v1.33.4"),
        )
    )

    with pytest.raises(UpgradeExecutionError) as excinfo:
        UpgradeExecutor(runner).run(REQUEST)

    assert excinfo.value.code == "UPGRADE_MULTI_CONTROL_PLANE_UNSUPPORTED"


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
