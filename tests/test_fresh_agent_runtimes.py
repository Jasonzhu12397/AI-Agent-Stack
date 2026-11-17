import hashlib
import json
from pathlib import Path

from apps.eph_agent import main as eph_main
from apps.eph_agent.main import execute as execute_eph
from apps.target_agent import main as target_main
from apps.target_agent.main import execute as execute_target


def test_eph_agent_does_not_execute_unregistered_mutations():
    result = execute_eph({"action": "capi.pivot"})
    assert result["status"] == "blocked"
    assert result["side_effects"] is False
    assert result["error_code"] == "ACTION_NOT_IMPLEMENTED"


def test_target_agent_does_not_execute_unregistered_mutations():
    result = execute_target({"action": "worker.provision"})
    assert result["status"] == "blocked"
    assert result["side_effects"] is False
    assert result["error_code"] == "ACTION_NOT_IMPLEMENTED"


def test_target_agent_rejects_unknown_action():
    result = execute_target({"action": "run.shell"})
    assert result["status"] == "blocked"
    assert result["side_effects"] is False



def test_target_agent_validates_worker_ntp_against_ironic_internal_ip():
    result = execute_target({
        "action": "target.worker.ntp.validate",
        "payload": {
            "ironic_internal_ip": "10.130.60.84",
            "ntp_sources": ["10.130.60.84"],
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["external_ntp_fallback_allowed"] is False


def test_target_agent_blocks_worker_external_ntp_fallback():
    result = execute_target({
        "action": "target.worker.ntp.validate",
        "payload": {
            "ironic_internal_ip": "10.130.60.84",
            "ntp_sources": ["10.130.60.84", "pool.ntp.org"],
        },
    })
    assert result["status"] == "blocked"
    assert result["side_effects"] is False



def test_fresh_agent_entrypoints_do_not_import_legacy_node_agent_runtime():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "apps/cloud_control_agent",
        root / "apps/eph_agent",
        root / "apps/target_agent",
        root / "packages/agent_transport",
    ]
    for path in paths:
        for source in path.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            assert "apps.node_agent" not in text
            assert "node_agent.runtime" not in text


def test_new_deployment_path_does_not_reference_legacy_node_agent_images():
    root = Path(__file__).resolve().parents[1]
    deployment = (root / "deploy/agents/deploy.sh").read_text(encoding="utf-8")
    build = (root / "deploy/agents/build-agents.sh").read_text(encoding="utf-8")
    assert "ai-agent-node-agent" not in deployment
    assert "Dockerfile.node-agent" not in build
    assert "network-inventory-agent" not in deployment



def test_target_agent_blocks_handoff_phase_skips():
    result = execute_target({
        "action": "target.handoff.evaluate",
        "payload": {
            "current_phase": "eph_single_node_ready",
            "next_phase": "bmh_registry_migrated",
            "evidence": {},
        },
    })
    assert result["status"] == "blocked"
    assert result["side_effects"] is False



def test_target_agent_cannot_claim_eph_owned_bootstrap_checkpoint():
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "not_started",
            "sequence": 0,
            "state": {},
        },
        "payload": {
            "current_phase": "target_control_plane_ready",
            "next_phase": "eph_single_node_ready",
            "evidence": {
                "eph_ready": True,
                "eph_node_count": 1,
                "eph_source_preflight_passed": True,
            },
        },
    })
    assert result["status"] == "blocked"
    assert result["error_code"] == "HANDOFF_EPH_CHECKPOINT_REQUIRED"
    assert result["side_effects"] is False


def test_target_agent_handoff_recognizes_already_persisted_checkpoint():
    evidence = {
        "eph_ready": True,
        "eph_node_count": 1,
        "eph_source_preflight_passed": True,
    }
    digest = hashlib.sha256(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "eph_single_node_ready",
            "sequence": 1,
            "state": {"state_digest": digest, "evidence": evidence},
        },
        "payload": {
            "next_phase": "eph_single_node_ready",
            "evidence": evidence,
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["already_applied"] is True



def test_target_agent_observes_live_metal3_ownership_before_checkpoint(monkeypatch):
    source_identities = [{
        "namespace": "metal3",
        "name": "host-1",
        "hardware_identity": "system-uuid:sha256:abc",
    }]
    observed = {
        "target_bmh_identities": source_identities,
        "target_metal3_ownership_verified": True,
        "target_metal3_reconciliation_healthy": True,
    }
    monkeypatch.setattr(
        target_main,
        "_observe_target_metal3_ownership",
        lambda payload: observed,
    )
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "capi_pivot_verified",
            "sequence": 4,
            "state": {},
        },
        "payload": {
            "namespace": "metal3",
            "cluster_name": "target-cluster",
            "next_phase": "bmh_registry_migrated",
            "evidence": {
                "source_bmh_identities": source_identities,
            },
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["evidence"]["target_metal3_ownership_verified"] is True



def test_target_agent_renders_control_plane_chronyd_profile_without_claiming_host_changes():
    result = execute_target({
        "action": "target.ntp.control_plane.render",
        "payload": {
            "configured_external_sources": ["192.0.2.10"],
            "approved_external_sources": ["192.0.2.10"],
            "worker_networks": ["10.130.60.80/28"],
        },
    })
    assert result["status"] == "succeeded"
    assert "server 192.0.2.10 iburst" in result["output"]["chrony_config"]
    assert "allow 10.130.60.80/28" in result["output"]["chrony_config"]
    assert result["output"]["host_configuration_applied"] is False
    assert result["side_effects"] is False



def test_target_agent_verifies_live_master_readiness_and_ntp_before_handoff(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_observe_control_plane_ntp_bootstrap",
        lambda _payload: {
            "target_control_plane_ntp_profile_verified": True,
            "target_control_plane_ntp_external_sync_verified": True,
        },
    )
    monkeypatch.setattr(target_main, "_observe_ready_master_count", lambda: 3)
    monkeypatch.setattr(
        target_main,
        "probe_ironic_internal_ntp_endpoint",
        lambda _ip: {"reachable": True, "stratum": 2, "port": 123},
    )
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "eph_single_node_ready",
            "sequence": 1,
            "state": {},
        },
        "payload": {
            "next_phase": "target_control_plane_ready",
            "evidence": {
                "ironic_internal_ntp_ip": "10.130.60.84",
            },
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["evidence"]["ready_master_count"] == 3
    assert result["output"]["evidence"]["ironic_internal_ntp_service_reachable"] is True
    assert result["output"]["evidence"]["target_control_plane_ntp_external_sync_verified"] is True



def test_target_agent_reads_worker_inventory_from_target_kubernetes_api(monkeypatch):
    observed = {
        "apiVersion": "v1",
        "kind": "NodeList",
        "items": [
            {
                "metadata": {
                    "name": "worker-01",
                    "uid": "node-uid-01",
                    "labels": {"node-role.kubernetes.io/worker": ""},
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ],
    }
    calls = []

    def fake_kubectl(arguments, **_kwargs):
        calls.append(arguments)
        return {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(observed),
            "stderr": "",
        }

    monkeypatch.setattr(target_main, "_kubectl", fake_kubectl)
    result = execute_target({"action": "target.cluster.observe"})

    assert result["status"] == "succeeded"
    assert json.loads(result["output"]["stdout"]) == observed
    assert calls == [["get", "nodes", "-o", "json"]]
    assert result["side_effects"] is False



def test_eph_preflight_emits_checkpoint_ready_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(tmp_path / "source.kubeconfig"))
    (tmp_path / "source.kubeconfig").write_text("test", encoding="utf-8")
    node_list = {
        "items": [{
            "metadata": {"name": "eph-single-node"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }]
    }

    def fake_read_only(command, timeout=20):
        if command[:3] == ["kubectl", "get", "nodes"]:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(node_list),
                "stderr": "",
            }
        return {
            "available": True,
            "returncode": 0,
            "stdout": "{}",
            "stderr": "",
        }

    monkeypatch.setattr(eph_main, "_run_read_only", fake_read_only)
    result = execute_eph({"action": "eph.preflight"})
    assert result["status"] == "succeeded"
    assert result["output"]["eph_ready"] is True
    assert result["output"]["eph_node_count"] == 1
    assert result["output"]["eph_source_preflight_passed"] is True



def test_target_agent_verifies_live_capi_pivot_annotations_and_cluster_readiness(monkeypatch):
    cluster = {
        "metadata": {
            "uid": "target-cluster-object-uid",
            "generation": 3,
            "annotations": {
                "ai-agent-stack.io/source-cluster-uid": "source-cluster-object-uid",
                "ai-agent-stack.io/stable-cluster-identity": "logical-cluster-001",
            },
        },
        "status": {
            "observedGeneration": 3,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda _arguments, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(cluster),
            "stderr": "",
        },
    )
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "target_agent_ready",
            "sequence": 2,
            "state": {},
        },
        "payload": {
            "next_phase": "capi_pivot_verified",
            "namespace": "metal3",
            "cluster_name": "target-cluster",
            "stable_cluster_identity": "logical-cluster-001",
            "evidence": {
                "source_cluster_uid": "source-cluster-object-uid",
                "source_cluster_absent": True,
                "target_controllers_ready": True,
            },
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["evidence"]["target_cluster_uid"] == "target-cluster-object-uid"
    assert result["output"]["evidence"]["target_cluster_ownership_verified"] is True


def test_target_agent_rejects_capi_pivot_when_live_target_source_uid_annotation_differs(
    monkeypatch,
):
    cluster = {
        "metadata": {
            "uid": "target-cluster-object-uid",
            "generation": 1,
            "annotations": {
                "ai-agent-stack.io/source-cluster-uid": "wrong-source-uid",
                "ai-agent-stack.io/stable-cluster-identity": "logical-cluster-001",
            },
        },
        "status": {
            "observedGeneration": 1,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda _arguments, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(cluster),
            "stderr": "",
        },
    )
    result = execute_target({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "target_agent_ready",
            "sequence": 2,
            "state": {},
        },
        "payload": {
            "next_phase": "capi_pivot_verified",
            "namespace": "metal3",
            "cluster_name": "target-cluster",
            "stable_cluster_identity": "logical-cluster-001",
            "evidence": {
                "source_cluster_uid": "source-cluster-object-uid",
                "source_cluster_absent": True,
                "target_controllers_ready": True,
            },
        },
    })
    assert result["status"] == "blocked"
    assert result["error_code"] == "TARGET_CAPI_PIVOT_SOURCE_UID_ANNOTATION_MISMATCH"
    assert result["side_effects"] is False
