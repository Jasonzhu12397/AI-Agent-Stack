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


def test_vendored_scripts_do_not_touch_the_cluster_themselves():
    launch = (VENDOR / "03_launch_mgmt_cluster.sh").read_text(encoding="utf-8")
    verify = (VENDOR / "04_verify.sh").read_text(encoding="utf-8")

    assert "METAL3_LAB_AGENT_BRIDGE" in launch
    assert "metal3_skill.apply_baremetalhosts" in launch
    # the direct kubectl path survives only as the explicit local driver
    assert 'kubectl apply -f "${hosts_dir}"' in launch
    assert 'METAL3_LAB_BMH_DRIVER=local' in launch

    assert '"${METAL3_LAB_AGENT_BRIDGE}" verify --expected "${NUM_NODES}"' in verify
    assert "observe_baremetalhosts" in verify
    # the bare metal branch never probes the cluster itself any more
    marker = 'if [[ "${NODES_PLATFORM}" == "baremetal" ]]; then'
    branch = verify.split(marker, 1)[1].split("else", 1)[0]
    assert "kubectl" not in branch
    assert "jq" not in branch


def test_runner_exports_the_agent_bridge():
    runner = (ROOT / "deploy" / "metal3-lab" / "run.sh").read_text(encoding="utf-8")
    assert "tools/metal3_agent_bridge.py" in runner
    assert 'export METAL3_LAB_AGENT_BRIDGE="${AGENT_BRIDGE}"' in runner
    assert 'METAL3_LAB_BMH_DRIVER="${METAL3_LAB_BMH_DRIVER:-agent}"' in runner
