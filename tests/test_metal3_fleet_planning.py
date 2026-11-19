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
