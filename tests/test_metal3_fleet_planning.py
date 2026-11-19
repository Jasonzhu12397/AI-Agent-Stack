"""The lab reasons about roles instead of assuming a flat host list.

A target cluster needs masters and workers, and getting that split wrong costs
a deployment. These tests pin the decision: explicit platform labels win, the
rest is ranked from reported facts, the control plane size is one of 1/3/5/7,
and every assignment carries the sentence that justified it.
"""

from __future__ import annotations

import json

import pytest

from apps.eph_agent import capi_renderer
from apps.eph_agent import fleet_planning as fp
from tools import metal3_deploy


def spec(name, *, cpu=8, ram_gib=32, disk=200, status="verified", labels=None):
    return fp.MachineSpec(
        name=name,
        machine_id=f"id-{name}",
        project_id="proj",
        bmc_address=f"redfish+https://10.0.0.1/redfish/v1/Systems/{name}",
        boot_mac="aa:bb:cc:dd:ee:01",
        secret_ref=f"{name}-bmc",
        cpu_cores=cpu,
        ram_mib=ram_gib * 1024,
        disk_gib=disk,
        labels=labels or {},
        identity_status=status,
    )


def test_bigger_machines_become_the_control_plane():
    fleet = [
        spec("small-1", cpu=4, ram_gib=16, disk=100),
        spec("big-1", cpu=32, ram_gib=128, disk=800),
        spec("big-2", cpu=32, ram_gib=128, disk=800),
        spec("mid-1", cpu=16, ram_gib=64, disk=400),
        spec("mid-2", cpu=16, ram_gib=64, disk=400),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=3)

    masters = {item.machine.name for item in plan.by_role(fp.ROLE_CONTROL_PLANE)}
    assert masters == {"big-1", "big-2", "mid-1"}
    assert plan.control_plane_count == 3
    assert {item.machine.name for item in plan.by_role(fp.ROLE_WORKER)} == {"mid-2", "small-1"}
    assert all(
        "ranked into the control plane" in item.reason
        for item in plan.by_role(fp.ROLE_CONTROL_PLANE)
    )


def test_explicit_role_labels_win_over_capacity():
    fleet = [
        spec("weak-master", cpu=2, ram_gib=8, disk=50, labels={fp.ROLE_LABEL: "control-plane"}),
        spec("strong-1", cpu=64, ram_gib=256, disk=1600),
        spec("strong-2", cpu=64, ram_gib=256, disk=1600),
        spec("strong-3", cpu=64, ram_gib=256, disk=1600),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)

    assert plan.control_plane_count == 1
    masters = [item.machine.name for item in plan.by_role(fp.ROLE_CONTROL_PLANE)]
    assert masters == ["weak-master"]
    assert "labelled" in plan.by_role(fp.ROLE_CONTROL_PLANE)[0].reason


def test_worker_pool_labels_group_the_data_plane():
    fleet = [
        spec("cp-1", labels={fp.ROLE_LABEL: "control-plane"}),
        spec("w-1", labels={fp.ROLE_LABEL: "worker", fp.POOL_LABEL: "gpu"}),
        spec("w-2", labels={fp.ROLE_LABEL: "worker", fp.POOL_LABEL: "gpu"}),
        spec("w-3", labels={fp.ROLE_LABEL: "worker"}),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)

    pools = plan.worker_pools
    assert sorted(pools) == ["gpu", fp.DEFAULT_WORKER_POOL]
    assert [item.machine.name for item in pools["gpu"]] == ["w-1", "w-2"]


def test_control_plane_shrinks_when_the_fleet_is_small():
    fleet = [spec(f"node-{index}") for index in range(2)]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=3)

    assert plan.control_plane_count == 1
    assert plan.warnings
    assert "only 2 machine(s) can serve" in plan.warnings[0]


def test_identity_conflicts_are_excluded_and_reported():
    fleet = [spec("good-1"), spec("conflicted", status="conflict"), spec("good-2")]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)

    assert all(item.machine.name != "conflicted" for item in plan.assignments)
    assert any("conflict" in warning for warning in plan.warnings)


def test_missing_facts_are_flagged_not_invented():
    fleet = [
        spec("quiet", cpu=0, ram_gib=0, disk=0, status="unverified"),
        spec("loud", cpu=32, ram_gib=128, disk=800),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)

    master = plan.by_role(fp.ROLE_CONTROL_PLANE)[0].machine.name
    assert master == "loud"
    quiet = [item for item in plan.assignments if item.machine.name == "quiet"][0]
    gaps = plan.missing_facts(quiet)
    assert "cpu_cores not reported" in gaps
    assert "identity_status is unverified" in gaps


def test_invalid_control_plane_size_is_rejected():
    with pytest.raises(fp.PlanningError, match="control_plane_count"):
        fp.plan_fleet([spec("a")], cluster_name="target", control_plane_count=2)


def test_empty_fleet_is_rejected():
    with pytest.raises(fp.PlanningError, match="empty"):
        fp.plan_fleet([], cluster_name="target")


def test_bmh_host_plan_feeds_the_agent_renderer():
    fleet = [
        spec("cp-1", labels={fp.ROLE_LABEL: "control-plane"}),
        spec("w-1", labels={fp.ROLE_LABEL: "worker", fp.POOL_LABEL: "gpu"}),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)
    documents = capi_renderer.render_bmh_manifests(fp.bmh_host_plan(plan))

    assert len(documents) == 2
    labels = {item["metadata"]["name"]: item["metadata"]["labels"] for item in documents}
    assert labels["cp-1"]["nodeType"] == "controlplane"
    assert labels["cp-1"]["node-pool-name"] == fp.CONTROL_PLANE_POOL
    assert labels["w-1"]["nodeType"] == "worker"
    assert labels["w-1"]["node-pool-name"] == "gpu"
    for document in documents:
        assert document["metadata"]["annotations"]["ai-agent-stack.io/role"]


def test_cluster_spec_counts_worker_pools():
    fleet = [
        spec("cp-1", labels={fp.ROLE_LABEL: "control-plane"}),
        spec("w-1", labels={fp.ROLE_LABEL: "worker", fp.POOL_LABEL: "gpu"}),
        spec("w-2", labels={fp.ROLE_LABEL: "worker", fp.POOL_LABEL: "gpu"}),
        spec("w-3", labels={fp.ROLE_LABEL: "worker"}),
    ]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)
    spec_document = fp.cluster_spec(
        plan,
        control_plane_endpoint="10.0.0.50",
        image_url="http://10.0.0.5/images/eph-live.iso",
        data_template={"meta_data": {}, "network_data": {"links": [], "networks": []}},
    )

    assert spec_document["control_plane_count"] == 1
    pools = {pool["name"]: pool["count"] for pool in spec_document["worker_pools"]}
    assert pools == {"gpu": 2, fp.DEFAULT_WORKER_POOL: 1}


def test_plan_document_records_every_reason():
    fleet = [spec("cp-1", labels={fp.ROLE_LABEL: "control-plane"}), spec("w-1")]
    plan = fp.plan_fleet(fleet, cluster_name="target", control_plane_count=1)

    document = json.loads(json.dumps(fp.plan_document(plan)))
    assert document["control_plane_count"] == 1
    assert {item["role"] for item in document["assignments"]} == {"control-plane", "worker"}
    assert all(item["reason"] for item in document["assignments"])


def test_count_command_reads_the_recorded_plan(tmp_path):
    (tmp_path / metal3_deploy.PLAN_FILE).write_text(
        json.dumps({"assignments": [{"machine": "a"}, {"machine": "b"}]}), encoding="utf-8"
    )
    assert metal3_deploy.main(["count", "--dir", str(tmp_path)]) == 0


def _request_machine(name, **overrides):
    value = {
        "name": name,
        "machine_id": f"id-{name}",
        "project_id": "proj",
        "bmc_address": f"redfish+https://10.0.0.1/redfish/v1/Systems/{name}",
        "boot_mac": "aa:bb:cc:dd:ee:01",
        "secret_ref": f"{name}-bmc",
        "cpu_cores": 16,
        "ram_mib": 64 * 1024,
        "disk_gib": 400,
        "identity_status": "verified",
        "online": True,
        "tls_verify": True,
        "labels": {},
    }
    value.update(overrides)
    return value


def test_fleet_request_returns_bmh_and_cluster_artifacts():
    payload = {
        "cluster_name": "target",
        "namespace": "metal3",
        "control_plane_count": 1,
        "image_url": "http://registry.local/eph/worker.raw",
        "control_plane_endpoint": "10.0.0.50:6443",
        "fleet": [
            _request_machine("strong", cpu_cores=32, ram_mib=128 * 1024),
            _request_machine("small", cpu_cores=8, ram_mib=16 * 1024),
        ],
    }
    evidence = fp.plan_fleet_request(payload)

    assert evidence["control_plane_count"] == 1
    assert evidence["worker_replicas"] == 1
    assert evidence["fleet_plan"]["assignments"][0]["reason"]
    assert {item["node_type"] for item in evidence["hosts"]} == {"controlplane", "worker"}
    assert evidence["cluster_spec"]["name"] == "target"
    assert evidence["cluster_spec"]["control_plane_endpoint"] == "10.0.0.50:6443"


def test_fleet_request_rejects_duplicate_or_malformed_machines():
    payload = {
        "cluster_name": "target",
        "fleet": [_request_machine("cp"), _request_machine("cp")],
    }
    with pytest.raises(fp.PlanningError, match="duplicate"):
        fp.plan_fleet_request(payload)

    payload["fleet"] = [_request_machine("cp", cpu_cores=-1)]
    with pytest.raises(fp.PlanningError, match="cpu_cores"):
        fp.plan_fleet_request(payload)


def test_eph_preflight_reasons_about_roles_and_records_the_plan(tmp_path, monkeypatch):
    from apps.eph_agent import main as eph_main

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("EPH_KUBECONFIG", str(kubeconfig))
    nodes = {
        "items": [{
            "metadata": {"name": "eph-bootstrap"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }]
    }

    def runner(command, **_kwargs):
        if command[:3] == ["kubectl", "get", "nodes"]:
            return {"available": True, "stdout": json.dumps(nodes), "stderr": ""}
        return {"available": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(eph_main, "_run_read_only", runner)
    result = eph_main.execute({
        "action": "eph.preflight",
        "payload": {
            "cluster_name": "target",
            "control_plane_count": 1,
            "image_url": "http://registry.local/worker.raw",
            "control_plane_endpoint": "10.0.0.50:6443",
            "fleet": [
                _request_machine("strong", cpu_cores=32, ram_mib=128 * 1024),
                _request_machine("small", cpu_cores=8, ram_mib=16 * 1024),
            ],
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["fleet_plan"]["control_plane_count"] == 1
    assert result["output"]["worker_replicas"] == 1
    assert len(result["output"]["hosts"]) == 2
    assert result["output"]["cluster_spec"]["name"] == "target"


def test_eph_preflight_blocks_on_an_invalid_fleet(tmp_path, monkeypatch):
    from apps.eph_agent import main as eph_main

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("EPH_KUBECONFIG", str(kubeconfig))
    monkeypatch.setattr(
        eph_main,
        "_run_read_only",
        lambda *_args, **_kwargs: {"available": True, "stdout": "{}", "stderr": ""},
    )
    result = eph_main.execute({
        "action": "eph.preflight",
        "payload": {"cluster_name": "target", "fleet": [{"name": "broken"}]},
    })
    assert result["status"] == "blocked"
    assert result["error_code"] == "EPH_FLEET_PLAN_INVALID"


def test_fleet_request_derives_a_data_template_from_the_physical_network():
    payload = {
        "cluster_name": "target",
        "control_plane_count": 1,
        "image_url": "http://registry.local/worker.raw",
        "control_plane_endpoint": "10.0.0.50:6443",
        "network_spec": {
            "control_plane_interface": "eno1",
            "dns_servers": ["10.0.0.53"],
        },
        "fleet": [
            _request_machine("cp", cpu_cores=32, ram_mib=128 * 1024),
            _request_machine("worker"),
        ],
    }
    evidence = fp.plan_fleet_request(payload)
    template = evidence["cluster_spec"]["control_plane_data_template"]
    assert template["network_data"]["links"]["ethernets"][0]["id"] == "eno1"
    assert template["network_data"]["services"]["dns"] == ["10.0.0.53"]
