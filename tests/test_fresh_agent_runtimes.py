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
