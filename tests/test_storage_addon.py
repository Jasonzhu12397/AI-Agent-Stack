from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.target_agent.storage_addon import (
    StorageAddonInstallError,
    execute_storage_addon,
)
from packages.agent_contracts.storage_addon import (
    StorageAddonError,
    plan_storage_addon,
    render_ceph_cluster,
    render_network_attachments,
    render_storage_resources,
)

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "deploy/offline/addons/rook-ceph"


def _worker_contract() -> dict[str, object]:
    return {
        "control_plane_interface": "bond0",
        "control_plane_cidr": "10.0.60.0/24",
        "data_plane_interface": "bond1",
        "data_plane_cidr": "10.0.61.0/24",
        "data_plane_mtu": 9000,
    }


def _addon() -> dict[str, object]:
    return {
        "enabled": True,
        "type": "rook-ceph",
        "rook_version": "v1.17.4",
        "ceph_image": "quay.io/ceph/ceph:v19.2.2",
        "namespace": "rook-ceph",
        "operator_namespace": "rook-ceph",
        "network": {
            "internal": {
                "name": "ceph-cluster-net",
                "interface": "eno5",
                "cidr": "192.168.60.0/24",
                "mtu": 9000,
                "vlan": 610,
                "ipam": "whereabouts",
            },
            "external": {
                "name": "ceph-public-net",
                "interface": "eno6",
                "cidr": "192.168.61.0/24",
                "mtu": 9000,
                "vlan": 611,
                "ipam": "whereabouts",
            },
        },
        "storage": {
            "nodes": [
                {"name": "worker-0", "devices": ["/dev/nvme0n1"]},
                {"name": "worker-1", "devices": ["/dev/nvme0n1"]},
                {"name": "worker-2", "devices": ["/dev/nvme0n1"]},
            ],
            "replication": 3,
            "failure_domain": "host",
        },
        "mon": {"count": 3},
        "mgr": {"count": 2},
        "storage_classes": {
            "block": True,
            "filesystem": True,
            "block_class": "ceph-rbd",
            "filesystem_class": "cephfs",
        },
        "timeout_seconds": 3600,
    }


def test_plan_normalizes_storage_addon() -> None:
    plan = plan_storage_addon(_addon(), worker_network_contract=_worker_contract())
    assert plan["enabled"] is True
    assert plan["network"]["internal"]["cidr"] == "192.168.60.0/24"
    assert plan["network"]["external"]["cidr"] == "192.168.61.0/24"
    assert plan["storage"]["nodes"][0]["devices"] == ["/dev/nvme0n1"]
    assert plan["storage_classes"]["block"] is True
    assert plan["storage_classes"]["filesystem"] is True


def test_disabled_addon_short_circuits() -> None:
    plan = plan_storage_addon({"enabled": False})
    assert plan == {"enabled": False, "type": "rook-ceph"}


@pytest.mark.parametrize(
    "mutate,code",
    [
        (
            lambda spec: spec["network"]["external"].update(
                {"cidr": "192.168.60.0/24"}
            ),
            "STORAGE_ADDON_EXTERNAL_CIDR_OVERLAP",
        ),
        (
            lambda spec: spec["network"]["internal"].update(
                {"interface": "bond1"}
            ),
            "STORAGE_ADDON_INTERNAL_INTERFACE_COLLISION",
        ),
        (
            lambda spec: spec["storage"]["nodes"].pop(),
            "STORAGE_ADDON_NODE_COUNT_BELOW_REPLICATION",
        ),
        (
            lambda spec: spec["mon"].update({"count": 2}),
            "STORAGE_ADDON_MON_COUNT_MUST_BE_ODD",
        ),
        (
            lambda spec: spec["storage"]["nodes"][0].update(
                {"devices": ["/etc/passwd"]}
            ),
            "STORAGE_ADDON_DEVICE_PATH_INVALID",
        ),
    ],
)
def test_plan_rejects_invalid_storage(mutate, code) -> None:
    spec = _addon()
    mutate(spec)
    with pytest.raises(StorageAddonError) as exc:
        plan_storage_addon(spec, worker_network_contract=_worker_contract())
    assert str(exc.value) == code


def test_storage_network_must_not_overlap_control_or_data() -> None:
    spec = _addon()
    spec["network"]["internal"]["cidr"] = "10.0.60.0/25"
    with pytest.raises(StorageAddonError) as exc:
        plan_storage_addon(spec, worker_network_contract=_worker_contract())
    assert str(exc.value) == "STORAGE_ADDON_INTERNAL_CIDR_OVERLAP"


def test_renders_multus_ceph_cluster_and_storage_classes() -> None:
    plan = plan_storage_addon(_addon(), worker_network_contract=_worker_contract())
    attachments = render_network_attachments(plan)
    assert [item["metadata"]["name"] for item in attachments] == [
        "ceph-cluster-net",
        "ceph-public-net",
    ]
    for document in attachments:
        config = json.loads(document["spec"]["config"])
        assert config["type"] == "macvlan"
        assert config["master"] in {"eno5", "eno6"}
    cluster = render_ceph_cluster(plan)
    assert cluster["kind"] == "CephCluster"
    assert cluster["spec"]["network"]["provider"] == "multus"
    assert cluster["spec"]["network"]["selectors"]["public"] == "ceph-public-net"
    assert cluster["spec"]["network"]["selectors"]["cluster"] == "ceph-cluster-net"
    resources = render_storage_resources(plan)
    assert [item["kind"] for item in resources] == [
        "CephBlockPool",
        "StorageClass",
        "CephFilesystem",
        "StorageClass",
    ]
    assert resources[1]["provisioner"] == "rook-ceph.rbd.csi.ceph.com"
    assert resources[3]["provisioner"] == "rook-ceph.cephfs.csi.ceph.com"


def test_execute_storage_addon_applies_and_waits_for_ready(monkeypatch) -> None:
    calls: list[list[str]] = []

    def runner(arguments, timeout=60, max_output_bytes=0, **_kwargs):
        calls.append(list(arguments))
        if arguments[0] == "get":
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "status": {
                            "phase": "Ready",
                            "ceph": {"health": "HEALTH_OK"},
                        }
                    }
                ),
            }
        return {"available": True, "returncode": 0, "stdout": ""}

    evidence = execute_storage_addon(
        _addon(),
        worker_network_contract=_worker_contract(),
        runner=runner,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        environ={"ROOK_CEPH_MANIFEST_DIR": str(MANIFEST_DIR)},
    )
    assert evidence["storage_addon_enabled"] is True
    assert evidence["storage_addon_ready"] is True
    assert evidence["storage_addon_version"] == "v1.17.4"
    assert evidence["ceph_cluster_phase"] == "Ready"
    assert evidence["ceph_health"] == "HEALTH_OK"
    assert evidence["storage_classes"] == ["ceph-rbd", "cephfs"]
    applies = [call for call in calls if call[0] == "apply"]
    assert len(applies) == 9
    assert not any("--dry-run=server" in call for call in applies[:3])


def test_execute_storage_addon_requires_worker_network_contract() -> None:
    with pytest.raises(StorageAddonInstallError) as exc:
        execute_storage_addon(
            _addon(),
            worker_network_contract=None,
            runner=lambda *_args, **_kwargs: {},
        )
    assert str(exc.value) == "STORAGE_ADDON_WORKER_NETWORK_CONTRACT_REQUIRED"


def test_execute_storage_addon_fails_on_ceph_failure_phase() -> None:
    def runner(arguments, timeout=60, max_output_bytes=0, **_kwargs):
        if arguments[0] == "get":
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({"status": {"phase": "Failed"}}),
            }
        return {"available": True, "returncode": 0, "stdout": ""}

    with pytest.raises(StorageAddonInstallError) as exc:
        execute_storage_addon(
            _addon(),
            worker_network_contract=_worker_contract(),
            runner=runner,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
            environ={"ROOK_CEPH_MANIFEST_DIR": str(MANIFEST_DIR)},
        )
    assert str(exc.value) == "STORAGE_ADDON_CEPHCLUSTER_FAILED"
    assert exc.value.side_effects is True


def test_execute_storage_addon_disabled_touches_nothing() -> None:
    def runner(*_args, **_kwargs):
        raise AssertionError("disabled addon must not call the cluster")

    evidence = execute_storage_addon(
        {"enabled": False},
        worker_network_contract=None,
        runner=runner,
    )
    assert evidence == {"storage_addon_enabled": False, "side_effects": False}
