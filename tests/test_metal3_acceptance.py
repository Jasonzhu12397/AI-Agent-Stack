
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
