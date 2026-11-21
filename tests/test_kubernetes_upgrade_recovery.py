"""The recovery report must describe a cluster without touching it."""

import json

import pytest
from agent_contracts.kubernetes_upgrade import (
    KubernetesUpgradeError,
    validate_recovery_request,
)
from target_agent.kubernetes_upgrade_exec import (
    DEFAULT_ETCD_SNAPSHOT_DIR,
    ETCD_SNAPSHOT_DIR_ENV,
    UpgradeExecutionError,
    UpgradeExecutor,
)


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


def _pods(*entries):
    return json.dumps(
        {
            "items": [
                {"metadata": {"name": name}, "spec": {"nodeName": node}}
                for name, node in entries
            ]
        }
    )


class RecoveryRunner:
    """A cluster mid-upgrade: the worker moved, the control plane did not."""

    def __init__(self, *, nodes, etcd_pods, etcd_healthy=True, listing=None, fail_on=()):
        self.calls = []
        self._nodes = nodes
        self._etcd_pods = etcd_pods
        self._healthy = etcd_healthy
        self._listing = listing
        self._fail_on = tuple(fail_on)

    def __call__(self, argv, timeout=20, **kwargs):
        self.calls.append(list(argv))
        if argv[:4] == ["kubectl", "get", "nodes", "-o"]:
            return {"available": True, "returncode": 0, "stdout": self._nodes, "stderr": ""}
        if "component=etcd" in " ".join(argv):
            return {"available": True, "returncode": 0, "stdout": self._etcd_pods, "stderr": ""}
        if argv and argv[0] == "sudo":
            if self._listing is None:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "no dir"}
            return {"available": True, "returncode": 0, "stdout": self._listing, "stderr": ""}
        for marker in self._fail_on:
            if marker in argv:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "boom"}
        if "health" in argv:
            if not self._healthy:
                return {
                    "available": True,
                    "returncode": 1,
                    "stdout": "https://10.0.0.10:2379 is unhealthy",
                    "stderr": "",
                }
            return {
                "available": True,
                "returncode": 0,
                "stdout": "https://10.0.0.10:2379 is healthy: ok",
                "stderr": "",
            }
        return {"available": True, "returncode": 0, "stdout": "ok", "stderr": ""}


CONTROL = ("cp-0", {"node-role.kubernetes.io/control-plane": ""}, "10.0.0.10", "v1.33.4")
WORKER = ("w-0", {}, "10.0.0.20", "v1.34.1")


def test_report_lists_drift_etcd_health_and_snapshots():
    listing = "snapshot-pre-v1.34.1-20261121T004500Z.db 4096 2026-11-21T00:46:00Z\n"
    runner = RecoveryRunner(
        nodes=_nodes(CONTROL, WORKER),
        etcd_pods=_pods(("etcd-cp-0", "cp-0")),
        listing=listing,
    )
    report = UpgradeExecutor(runner).report_recovery("v1.34.1")

    assert [row["name"] for row in report["nodes"]] == ["cp-0", "w-0"]
    assert [row["on_target"] for row in report["nodes"]] == [False, True]
    assert report["drift"] == 1
    assert report["nodes"][0]["role"] == "control-plane"
    assert report["etcd"]["healthy"] is True
    assert report["etcd"]["members"] == [
        {
            "pod": "etcd-cp-0",
            "node": "cp-0",
            "healthy": True,
            "detail": "https://10.0.0.10:2379 is healthy: ok",
        }
    ]
    assert report["snapshots"]["entries"] == [
        {
            "name": "snapshot-pre-v1.34.1-20261121T004500Z.db",
            "bytes": 4096,
            "taken": "2026-11-21T00:46:00Z",
        }
    ]
    assert report["warnings"] == []
    assert not [call for call in runner.calls if call[:2] == ["kubectl", "drain"]]


def test_report_degrades_when_etcd_and_the_snapshot_dir_are_unreadable():
    runner = RecoveryRunner(nodes=_nodes(CONTROL), etcd_pods=json.dumps({"items": []}))
    report = UpgradeExecutor(runner).report_recovery("v1.34.1")

    assert report["drift"] == 1
    assert report["snapshots"]["directory"] == DEFAULT_ETCD_SNAPSHOT_DIR
    assert report["snapshots"]["error"]
    assert "is unreadable" in report["warnings"][0]


def test_report_honours_the_configured_snapshot_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(ETCD_SNAPSHOT_DIR_ENV, str(tmp_path))
    runner = RecoveryRunner(
        nodes=_nodes(CONTROL),
        etcd_pods=_pods(("etcd-cp-0", "cp-0")),
        etcd_healthy=False,
        listing="",
    )
    report = UpgradeExecutor(runner).report_recovery("v1.34.1")

    assert report["snapshots"]["directory"] == str(tmp_path)
    assert report["snapshots"]["entries"] == []
    assert report["etcd"]["healthy"] is False
    assert "not healthy" in report["warnings"][0]


def test_report_refuses_to_invent_what_it_cannot_read():
    class Unreadable(RecoveryRunner):
        def __call__(self, argv, timeout=20, **kwargs):
            self.calls.append(list(argv))
            return {"available": True, "returncode": 1, "stdout": "", "stderr": "forbidden"}

    with pytest.raises(UpgradeExecutionError) as raised:
        UpgradeExecutor(Unreadable(nodes="", etcd_pods="")).report_recovery("v1.34.1")
    assert raised.value.code == "RECOVERY_NODES_UNREADABLE"


def test_recovery_request_only_accepts_a_version_shape():
    assert validate_recovery_request({}) == {"target_version": ""}
    assert validate_recovery_request({"target_version": "v1.34.1"}) == {
        "target_version": "v1.34.1"
    }
    with pytest.raises(KubernetesUpgradeError) as raised:
        validate_recovery_request({"target_version": "v1.34; rm -rf /"})
    assert raised.value.code == "RECOVERY_TARGET_VERSION_INVALID"
