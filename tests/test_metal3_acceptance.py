
import json

from apps.eph_agent.fleet_planning import MachineSpec
from tools import metal3_acceptance
from tools.metal3_fleet_source import PlatformMachine


def machine(name: str, mac: str = "aa:bb:cc:dd:ee:ff", cpu=16, ram=32768) -> PlatformMachine:
    return PlatformMachine(
        spec=MachineSpec(
            name=name,
            machine_id=name,
            project_id="project",
            bmc_address=f"https://bmc-{name}.lab/redfish/v1/Systems/1",
            boot_mac=mac,
            secret_ref=f"{name}-bmc",
            cpu_cores=cpu,
            ram_mib=ram,
            disk_gib=480,
            labels={},
            identity_status="verified",
            online=True,
            tls_verify=False,
        ),
        username="bmc-user",
        password="bmc-pass",
        bmc_credential_ref=f"{name}-bmc-ref",
    )


def test_structural_checks_report_skipped_and_thin_machines():
    payload = metal3_acceptance.structural_checks(
        [machine("node-1"), machine("node-2", mac="bad", cpu=2, ram=4096)],
        ["node-3: bmc_endpoint is not configured"],
    )
    statuses = [(item["name"], item["status"]) for item in payload["checks"]]
    assert ("fleet.planning_input", "fail") in statuses
    assert ("machine.structure", "pass") in statuses
    assert ("machine.structure", "fail") in statuses
    assert metal3_acceptance.overall_status(payload["checks"]) == "fail"


def test_redfish_probe_compares_boot_mac(monkeypatch):
    items = {
        "/redfish/v1/Systems/1": {
            "status": 200,
            "json": {
                "PowerState": "On",
                "Boot": {
                    "BootSourceOverrideTarget": "Pxe",
                    "BootSourceOverrideEnabled": "Once",
                },
                "EthernetInterfaces": {
                    "@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces"
                },
            },
        },
        "/redfish/v1/Systems/1/EthernetInterfaces": {
            "status": 200,
            "json": {"Members": [{"MACAddress": "AA:BB:CC:DD:EE:FF"}]},
        },
    }

    def fake_get(machine_record, path, *, timeout=10):
        return items[path]

    monkeypatch.setattr(metal3_acceptance, "_redfish_get", fake_get)
    result = metal3_acceptance.probe_redfish(machine("node-1"))
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["redfish.reachable"]["status"] == "pass"
    assert checks["redfish.boot_mac"]["status"] == "pass"
    assert checks["redfish.power_state"]["power_state"] == "On"


def test_verify_cluster_checks_replicas_and_data_plane():
    def payload(items):
        return {"ok": True, "payload": {"items": items}}

    snapshot = {
        "baremetalhosts": payload(
            [
                {
                    "metadata": {"name": "node-1"},
                    "status": {
                        "operationalStatus": "OK",
                        "provisioning": {"state": "provisioned"},
                    },
                }
            ]
        ),
        "machines": payload([]),
        "control_planes": payload([{"status": {"readyReplicas": 3}}]),
        "machine_deployments": payload([{"status": {"readyReplicas": 4}}]),
        "nodes": payload(
            [
                {
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    }
                }
            ]
        ),
        "calico": payload(
            [
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "env": [
                                            {
                                                "name": "FELIX_BPFENABLED",
                                                "value": "true",
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            ]
        ),
        "kube_proxy": payload([]),
    }
    checks = metal3_acceptance.verify_cluster(
        snapshot,
        expected_machines=["node-1"],
        expected_control_plane=3,
        expected_workers=4,
    )["checks"]
    statuses = {item["name"]: item["status"] for item in checks}
    assert statuses["cluster.bmh_inventory"] == "pass"
    assert statuses["cluster.bmh_provisioned"] == "pass"
    assert statuses["cluster.control_plane_replicas"] == "pass"
    assert statuses["cluster.worker_replicas"] == "pass"
    assert statuses["target.calico_ebpf"] == "pass"
    assert statuses["target.kube_proxy_absent"] == "pass"


def test_verify_cluster_fails_unprovisioned_bmh_and_missing_proxy_state():
    def payload(items):
        return {"ok": True, "payload": {"items": items}}

    snapshot = {
        "baremetalhosts": payload(
            [
                {
                    "metadata": {"name": "node-1"},
                    "status": {
                        "operationalStatus": "error",
                        "provisioning": {"state": "inspecting"},
                    },
                }
            ]
        ),
        "machines": payload([]),
        "control_planes": payload([{"status": {"readyReplicas": 1}}]),
        "machine_deployments": payload([{"status": {"readyReplicas": 0}}]),
    }
    checks = metal3_acceptance.verify_cluster(
        snapshot,
        expected_machines=["node-1", "node-2"],
        expected_control_plane=3,
        expected_workers=4,
    )["checks"]
    statuses = {item["name"]: item["status"] for item in checks}
    assert statuses["cluster.bmh_inventory"] == "fail"
    assert statuses["cluster.bmh_provisioned"] == "fail"
    assert statuses["cluster.control_plane_replicas"] == "fail"
    assert statuses["cluster.worker_replicas"] == "fail"
    assert metal3_acceptance.overall_status(checks) == "fail"


def test_rollback_readiness_turns_executor_checks_into_evidence(monkeypatch, tmp_path):
    from target_agent import kubernetes_rollback_exec

    assessed = {
        "control_planes": 1,
        "control_plane": "cp-0",
        "etcd_pod": "etcd-cp-0",
        "refusal": "",
        "blocked": False,
        "checks": [
            {"name": "cluster.control_plane.count", "ok": True, "detail": "1 control plane(s)"},
            {
                "name": "etcd.snapshot.verified",
                "ok": True,
                "detail": "snapshot status returned its hash",
            },
        ],
    }
    monkeypatch.setattr(
        kubernetes_rollback_exec.RollbackExecutor,
        "assess",
        lambda self, request: assessed,
    )
    out = tmp_path / "rollback.json"
    code = metal3_acceptance.main(
        [
            "rollback",
            "--kubeconfig",
            str(tmp_path / "target.kubeconfig"),
            "--snapshot",
            "/var/lib/etcd/backups/snapshot-pre-v1.34.1.db",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["kind"] == "metal3-rollback-readiness"
    assert payload["status"] == "pass"
    assert payload["control_planes"] == 1
    assert [check["name"] for check in payload["checks"]] == [
        "cluster.control_plane.count",
        "etcd.snapshot.verified",
    ]
    assert payload["local_node"] == "cp-0"
    # The approver sees the exact five stages before approving anything.
    assert [step["name"] for step in payload["steps"]] == [
        "preflight",
        "etcd_restore",
        "data_swap",
        "kubelet_restart",
        "postflight",
    ]


def test_rollback_evidence_lists_every_member_of_a_stacked_etcd(monkeypatch, tmp_path):
    from target_agent import kubernetes_rollback_exec

    assessed = {
        "control_planes": 3,
        "control_plane": "",
        "etcd_pod": "",
        "refusal": "",
        "blocked": False,
        "local_node": "cp-0",
        "members": [
            {"name": "cp-0", "node": "cp-0", "pod": "etcd-cp-0",
             "peer_url": "https://10.0.0.10:2380", "address": "10.0.0.10"},
            {"name": "cp-1", "node": "cp-1", "pod": "etcd-cp-1",
             "peer_url": "https://10.0.0.11:2380", "address": "10.0.0.11"},
            {"name": "cp-2", "node": "cp-2", "pod": "etcd-cp-2",
             "peer_url": "https://10.0.0.12:2380", "address": "10.0.0.12"},
        ],
        "checks": [
            {"name": "cluster.control_plane.count", "ok": True, "detail": "3 control plane(s)"},
        ],
    }
    monkeypatch.setattr(
        kubernetes_rollback_exec.RollbackExecutor,
        "assess",
        lambda self, request: assessed,
    )
    out = tmp_path / "rollback.json"
    code = metal3_acceptance.main(
        [
            "rollback",
            "--kubeconfig",
            str(tmp_path / "target.kubeconfig"),
            "--snapshot",
            "/var/lib/etcd/backups/snapshot-pre-v1.34.1.db",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text())
    assert [item["node"] for item in payload["members"]] == ["cp-0", "cp-1", "cp-2"]
    assert payload["members"][1]["peer_url"] == "https://10.0.0.11:2380"
    restore = next(step for step in payload["steps"] if step["name"] == "etcd_restore")
    # One restore per member, each naming its own member and peer URL.
    assert sum(1 for command in restore["commands"] if "--initial-cluster=" in command) == 3
