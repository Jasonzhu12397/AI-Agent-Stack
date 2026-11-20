from apps.eph_agent import main


def _document():
    return {
        "interfaces": [
            {"name": "bond_control", "type": "bond", "state": "up"},
            {"name": "bond_control.70", "type": "vlan", "state": "up"},
        ],
        "routes": {"config": []},
    }


def test_eph_network_configure_requires_document():
    result = main.execute({"action": "eph.network.configure", "payload": {}})
    assert result["status"] == "failed"
    assert result["error_code"] == "EPH_NETWORK_DOCUMENT_INVALID"
    assert result["side_effects"] is False


def test_eph_network_configure_blocks_without_nmstate(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("nmstatectl")

    monkeypatch.setattr(main.subprocess, "run", missing)
    result = main.execute(
        {"action": "eph.network.configure", "payload": {"eph_network": _document()}}
    )
    assert result["status"] == "blocked"
    assert result["error_code"] == "EPH_NETWORK_NMSTATE_UNAVAILABLE"
    assert result["side_effects"] is False


def test_eph_network_configure_reports_apply_failure(monkeypatch):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "nmstate: conflict"

    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: Completed())
    result = main.execute(
        {"action": "eph.network.configure", "payload": {"eph_network": _document()}}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "EPH_NETWORK_APPLY_FAILED"
    assert "conflict" in result["output"]["stderr"]


def test_eph_network_configure_applies_document(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "Desired state applied"
        stderr = ""

    calls = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        return Completed()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    result = main.execute(
        {"action": "eph.network.configure", "payload": {"eph_network": _document()}}
    )
    assert result["status"] == "succeeded"
    assert result["output"]["applied_interfaces"] == ["bond_control", "bond_control.70"]
    assert calls and calls[0][0] == "nmstatectl"
    assert calls[0][1] == "apply"
    assert calls[0][2].endswith(".nmstate.yml")
