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
