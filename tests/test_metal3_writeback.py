"""The plan the agent reasoned about is mirrored back into the platform.

A second, stale opinion about which machine is a master is worse than none, so
the write-back copies exactly what the planner decided and audits the decision.
"""

from __future__ import annotations

import json

import pytest
from core.models import (
    AgentMachine,
    AgentNode,
    AuditEvent,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model

from apps.eph_agent import fleet_planning as fp
from tools import metal3_deploy
from tools.metal3_writeback import write_back_plan

RESOLVER = "tests.test_metal3_platform_inventory.stub_resolver"


def stub_resolver(**kwargs):
    return {"username": "admin", "password": "secret"}


@pytest.fixture
def fleet(db):
    user = get_user_model().objects.create_user(username="wb-owner", password="test-password")
    tenant = Tenant.objects.create(name="WB tenant", slug="wb-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="WB project", slug="wb-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)

    machines = []
    for name, role, pool in (
        ("wb-cp-1", "control-plane", "control-plane"),
        ("wb-w-1", "worker", "gpu"),
    ):
        node = AgentNode.objects.create(
            tenant=tenant,
            project=project,
            created_by=user,
            name=name,
            node_type=AgentNode.NodeType.EPH_NODE,
            credential_hash="d" * 64,
            labels={fp.ROLE_LABEL: role, fp.POOL_LABEL: pool},
        )
        machine = AgentMachine.objects.create(
            tenant=tenant,
            project=project,
            agent_node=node,
            hardware_inventory={
                "status": "observed",
                "boot_mac": "aa:bb:cc:dd:ee:01",
                "cpu": 16,
                "ram_mib": 65536,
            },
            bmc_endpoint="https://10.0.0.11",
            bmc_credential_ref="vault/kv/bmc/wb",
        )
        machines.append(machine)
    return tenant, project, machines


def specs(machines):
    return [
        fp.MachineSpec(
            name=machine.agent_node.name,
            machine_id=str(machine.id),
            project_id=str(machine.project_id),
            bmc_address="redfish+https://10.0.0.11/redfish/v1/Systems/1",
            boot_mac="aa:bb:cc:dd:ee:01",
            secret_ref=f"{machine.agent_node.name}-bmc",
            cpu_cores=16,
            ram_mib=65536,
            disk_gib=400,
            labels=dict(machine.agent_node.labels or {}),
            identity_status="verified",
        )
        for machine in machines
    ]


def test_write_back_mirrors_roles_and_audits(fleet):
    tenant, project, machines = fleet
    plan = fp.plan_fleet(specs(machines), cluster_name="target", control_plane_count=1)

    result = write_back_plan(plan)

    assert result["updated"] == 2
    assert result["audited"] == 2
    for machine in machines:
        machine.refresh_from_db()
        assert machine.bmh_name == machine.agent_node.name
    control_plane = AgentMachine.objects.get(agent_node__name="wb-cp-1")
    worker = AgentMachine.objects.get(agent_node__name="wb-w-1")
    assert control_plane.lifecycle_state == "planned_control_plane"
    assert worker.lifecycle_state == "planned_worker"

    events = AuditEvent.objects.filter(event_type="fleet.machine.role_planned")
    assert events.count() == 2
    metadata = events.first().metadata
    assert metadata["cluster_name"] == "target"
    assert metadata["control_plane_count"] == 1
    assert metadata["role"] in {"control-plane", "worker"}
    assert metadata["reason"]


def test_write_back_is_idempotent(fleet):
    tenant, project, machines = fleet
    plan = fp.plan_fleet(specs(machines), cluster_name="target", control_plane_count=1)

    first = write_back_plan(plan)
    second = write_back_plan(plan)

    assert first["updated"] == 2
    assert second["updated"] == 0
    # the audit trail still records the second run: the decision was re-confirmed
    assert second["audited"] == 2


def test_network_command_writes_the_plugin_plan(tmp_path):
    assert metal3_deploy.main(["network", "--dir", str(tmp_path)]) == 0
    document = json.loads((tmp_path / metal3_deploy.NETWORK_FILE).read_text(encoding="utf-8"))
    assert document["base_cni"] == "calico"
    enabled = [item for item in document["plugins"] if item["enabled"]]
    assert [item["name"] for item in enabled] == ["calico"]
    assert document["data_plane_plugins"] == ["dpdk", "ovs", "sriov"]


def test_network_command_honours_an_explicit_spec(tmp_path):
    spec = tmp_path / "network.json"
    spec.write_text(json.dumps({"plugins": {"sriov": {"enabled": True}}}), encoding="utf-8")

    assert metal3_deploy.main(["network", "--dir", str(tmp_path), "--spec", str(spec)]) == 0
    document = json.loads((tmp_path / metal3_deploy.NETWORK_FILE).read_text(encoding="utf-8"))
    enabled = {item["name"] for item in document["plugins"] if item["enabled"]}
    assert enabled == {"calico", "sriov"}


def test_stages_reports_progress_from_artifacts(tmp_path, capsys):
    (tmp_path / metal3_deploy.PLAN_FILE).write_text(
        json.dumps(
            {
                "cluster_name": "target",
                "namespace": "metal3",
                "control_plane_count": 1,
                "assignments": [{"role": "control-plane"}, {"role": "worker"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / metal3_deploy.BMH_FILE).write_text("kind: BareMetalHost\n", encoding="utf-8")

    assert metal3_deploy.main(["stages", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "done     control_plane" in out
    assert "pending  pivot" in out

    (tmp_path / "attestation.json").write_text("{}", encoding="utf-8")
    assert metal3_deploy.main(["stages", "--dir", str(tmp_path)]) == 0
    assert "done     pivot" in capsys.readouterr().out
