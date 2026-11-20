from apps.eph_agent import main


def _command(phase="eph_single_node_ready", document=None):
    return {
        "action": "eph.controlstack.apply",
        "deployment_checkpoint": {"phase": phase},
        "payload": {
            "spec": {
                "cluster_name": "edge-lab",
                "ironic": {
                    "api_credentials_name": "ironic-credentials",
                    "version": "30.0.0",
                    "images": {
                        "ironic": "registry.local/ironic:30.0.0",
                        "deploy_ramdisk_downloader": "registry.local/ramdisk:30.0.0",
                        "keepalived": "registry.local/keepalived:2.0.0",
                    },
                    "networking": {
                        "interface": "bond_control",
                        "ip_address": "172.18.0.3",
                    },
                },
            },
            "eph_network": document
            or {
                "interfaces": [
                    {"name": "bond_control", "type": "bond", "state": "up"}
                ]
            },
        },
    }


def test_controlstack_applies_eph_network_before_manifests(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = "applied"
        stderr = ""

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        return Completed()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(
        main,
        "plan_control_stack",
        lambda spec: [],
    )
    monkeypatch.setattr(
        main,
        "load_component_manifests",
        lambda names: {},
    )
    monkeypatch.setattr(
        main,
        "apply_control_stack",
        lambda plan, runner, lease, manifests: {"side_effects": True},
    )
    monkeypatch.setattr(
        main,
        "wait_for_control_stack_ready",
        lambda plan, runner, lease: {"ready": True},
    )
    result = main.execute(_command())
    assert result["status"] == "succeeded"
    assert result["output"]["network_interfaces"] == ["bond_control"]
    assert calls and calls[0][:2] == ["nmstatectl", "apply"]


def test_controlstack_blocks_when_nmstate_is_missing(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("nmstatectl")

    monkeypatch.setattr(main.subprocess, "run", missing)
    result = main.execute(_command())
    assert result["status"] == "blocked"
    assert result["error_code"] == "EPH_NETWORK_NMSTATE_UNAVAILABLE"
    assert result["side_effects"] is False
