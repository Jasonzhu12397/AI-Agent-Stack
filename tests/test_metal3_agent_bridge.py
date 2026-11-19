"""BMH registration and readiness judgement belong to the agent, not the shell.

The shell scripts must not apply manifests or parse cluster JSON themselves.
These tests pin that seam: the scripts call tools/metal3_agent_bridge.py, which
drives apps/eph_agent/metal3_skill, and only the agent's verdict becomes a
script result.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.metal3_agent_bridge as bridge  # noqa: E402
from apps.eph_agent import metal3_skill  # noqa: E402

VENDOR = ROOT / "deploy" / "metal3-lab" / "vendor" / "metal3-dev-env"


def host_observation(*, status="OK", state="available", error=""):
    return {
        "namespace": "metal3",
        "host_count": 2,
        "hosts": [
            {
                "name": "bm-node-1",
                "provisioning_state": state,
                "operational_status": status,
                "error_message": error,
            },
            {
                "name": "bm-node-2",
                "provisioning_state": state,
                "operational_status": status,
                "error_message": error,
            },
        ],
    }


def test_verify_accepts_a_ready_inventory_through_the_agent(monkeypatch, capsys):
    fake = lambda payload, runner: host_observation()  # noqa: E731
    monkeypatch.setattr(metal3_skill, "observe_baremetalhosts", fake)

    assert bridge.main(["verify", "--expected", "2"]) == 0
    out = capsys.readouterr().out
    assert "OK   bm-node-1" in out
    assert "OK   bm-node-2" in out


def test_verify_fails_when_a_host_is_not_operational(monkeypatch, capsys):
    fake = lambda payload, runner: host_observation(status="Error")  # noqa: E731
    monkeypatch.setattr(metal3_skill, "observe_baremetalhosts", fake)

    assert bridge.main(["verify", "--expected", "2"]) == 1
    err = capsys.readouterr().err
    assert "operationalStatus is Error" in err


def test_verify_fails_when_introspection_is_incomplete(monkeypatch, capsys):
    fake = lambda payload, runner: host_observation(state="inspecting")  # noqa: E731
    monkeypatch.setattr(metal3_skill, "observe_baremetalhosts", fake)

    assert bridge.main(["verify", "--expected", "2"]) == 1
    assert "expected one of" in capsys.readouterr().err


def test_verify_fails_when_the_machine_count_does_not_match(monkeypatch, capsys):
    fake = lambda payload, runner: host_observation()  # noqa: E731
    monkeypatch.setattr(metal3_skill, "observe_baremetalhosts", fake)

    assert bridge.main(["verify", "--expected", "3"]) == 1
    assert "the agent sees 2" in capsys.readouterr().err


def test_apply_splits_secrets_from_hosts_and_delegates_to_the_agent(monkeypatch, tmp_path):
    bundle = tmp_path / "bmhs.yaml"
    bundle.write_text(
        yaml.safe_dump_all(
            [
                {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "bm-node-1-bmc"}},
                {
                    "apiVersion": "metal3.io/v1alpha1",
                    "kind": "BareMetalHost",
                    "metadata": {"name": "bm-node-1"},
                    "spec": {"bmc": {"credentialsName": "bm-node-1-bmc"}},
                },
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    calls = {}

    def fake_apply(payload, runner, lease):
        calls["payload"] = payload
        return {"server_side_apply_succeeded": True}

    monkeypatch.setattr(metal3_skill, "apply_baremetalhosts", fake_apply)

    def ok_runner(arguments, timeout=60, **_):
        return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "_StdinKubectl", lambda documents: ok_runner)

    assert bridge.main(["apply", "--manifest", str(bundle), "--namespace", "metal3"]) == 0
    assert [item["kind"] for item in calls["payload"]["manifests"]] == ["BareMetalHost"]


def test_apply_rejects_a_bundle_without_hosts(monkeypatch, tmp_path):
    bundle = tmp_path / "empty.yaml"
    bundle.write_text("apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        bridge.main(["apply", "--manifest", str(bundle), "--namespace", "metal3"])


def test_vendored_scripts_do_not_touch_the_target_cluster():
    launch = (VENDOR / "03_launch_mgmt_cluster.sh").read_text(encoding="utf-8")

    # The management bootstrap stops after its controllers roll out. It neither
    # renders nor applies BMHs, and it does not invoke another orchestrator.
    assert "apply_bm_hosts" not in launch
    assert "metal3_deploy" not in launch
    assert "make-bm-worker" not in launch
    assert not (VENDOR / "04_verify.sh").exists()


def test_python_orchestrator_calls_the_agent_deploy_pipeline_directly():
    runner = (ROOT / "apps" / "eph_agent" / "metal3_lab.py").read_text(encoding="utf-8")
    assert "from tools import metal3_deploy, vendor_import" in runner
    assert "self.deploy(arguments)" in runner
    assert "shell=True" not in runner
    assert "metal3_agent_bridge" not in runner
