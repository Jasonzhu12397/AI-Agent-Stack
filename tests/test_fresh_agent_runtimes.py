import hashlib
import json
from pathlib import Path

from apps.eph_agent.main import execute as execute_eph
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



def test_target_agent_handoff_uses_cloud_checkpoint_not_payload_phase():
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
    assert result["status"] == "succeeded"
    assert result["output"]["from_phase"] is None
    assert result["output"]["to_phase"] == "eph_single_node_ready"


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
