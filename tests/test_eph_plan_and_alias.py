"""The EPH terminal commands the platform authorizes must produce evidence."""

import eph_agent.main as eph
import pytest

MACHINE = {
    "name": "master-0",
    "machine_id": "00000000-0000-0000-0000-000000000001",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "bmc_address": "10.0.0.11",
    "boot_mac": "aa:bb:cc:dd:ee:01",
    "secret_ref": "vault/bmc/rack-1",
    "cpu_cores": 24,
    "ram_mib": 65536,
    "disk_gib": 480,
    "identity_status": "verified",
}


def _command(action, payload):
    return {
        "action": action,
        "payload": payload,
        "command_id": "probe",
        "lease": {"lease_id": "probe", "token": "probe"},
    }


def test_cluster_plan_returns_a_fleet_plan():
    outcome = eph.execute(
        _command(
            "cluster.plan",
            {
                "cluster_name": "edge-lab",
                "control_plane_count": 1,
                "worker_replicas": 0,
                "fleet": [MACHINE],
            },
        )
    )

    assert outcome["status"] == "succeeded"
    assert outcome["side_effects"] is False
    assert outcome["output"]


def test_cluster_plan_fails_closed_when_the_fleet_is_too_small():
    outcome = eph.execute(
        _command(
            "cluster.plan",
            {
                "cluster_name": "edge-lab",
                "control_plane_count": 3,
                "worker_replicas": 0,
                "fleet": [MACHINE],
            },
        )
    )

    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "FLEET_PLAN_REJECTED"


def test_capi_cluster_observe_alias_reaches_the_capi_observer(monkeypatch):
    calls = []

    def fake_observer(payload, runner):
        calls.append(payload)
        return {"control_plane_ready": True}

    monkeypatch.setattr(eph, "observe_capi_cluster", fake_observer)

    outcome = eph.execute(_command("capi.cluster.observe", {"cluster_name": "edge-lab"}))

    assert outcome["status"] == "succeeded"
    assert calls == [{"cluster_name": "edge-lab"}]


@pytest.mark.parametrize("action", ["cluster.health", "k8s.resource.read", "node.health"])
def test_readonly_diagnostics_never_claim_side_effects(action, monkeypatch):
    monkeypatch.setattr(
        eph,
        "_run_read_only",
        lambda *a, **k: {"available": False, "returncode": 127, "stdout": "", "stderr": "stub"},
    )

    payload = {"resource": "nodes"} if action == "k8s.resource.read" else {}
    outcome = eph.execute(_command(action, payload))

    assert outcome["side_effects"] is False
    assert outcome["error_code"] in (None, "KUBECTL_UNAVAILABLE", "AGENT_RUNNER_INVALID")
