import base64
import json

import pytest
import yaml

from apps.eph_agent import data_template, metal3_lab
from tools import metal3_deploy


def test_default_data_template_uses_declared_physical_interface():
    result = data_template.default_data_template(
        {"control_plane_interface": "eno1", "dns_servers": ["10.0.0.53"]}
    )
    assert result["network_data"]["links"]["ethernets"] == [{"type": "phy", "id": "eno1"}]
    assert result["network_data"]["networks"]["ipv4DHCP"] == [
        {"id": "control-plane", "link": "eno1"}
    ]
    assert result["network_data"]["services"]["dns"] == ["10.0.0.53"]


def test_default_data_template_never_guesses_an_interface():
    with pytest.raises(data_template.DataTemplateError) as error:
        data_template.default_data_template({})
    assert str(error.value) == "METAL3_CONTROL_PLANE_INTERFACE_REQUIRED"


def test_target_kubeconfig_waits_for_capi_secret_and_writes_private_file(
    tmp_path, monkeypatch
):
    (tmp_path / metal3_deploy.PLAN_FILE).write_text(
        json.dumps({"cluster_name": "target", "namespace": "metal3"}), encoding="utf-8"
    )
    kubeconfig = yaml.safe_dump({"apiVersion": "v1", "clusters": [{"name": "target"}]})
    secret = {"data": {"value": base64.b64encode(kubeconfig.encode()).decode()}}
    monkeypatch.setattr(
        metal3_deploy,
        "_plain_runner",
        lambda: lambda *args, **kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(secret),
        },
    )
    assert metal3_deploy.main(["target-kubeconfig", "--dir", str(tmp_path)]) == 0
    destination = tmp_path / metal3_deploy.TARGET_KUBECONFIG_FILE
    assert yaml.safe_load(destination.read_text())["clusters"][0]["name"] == "target"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_network_apply_waits_and_records_ready_evidence(tmp_path, monkeypatch):
    spec = tmp_path / "network.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "control_plane_interface": "eno1",
                "manifests": {
                    "calico": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": "calico-kube-controllers"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metal3_deploy.network_stack_adapter,
        "apply_network_stack",
        lambda *args, **kwargs: {
            "apply_pending_plugins": [],
            "applied_plugins": ["calico"],
            "side_effects": True,
        },
    )
    monkeypatch.setattr(
        metal3_deploy.network_stack_adapter,
        "wait_for_network_stack_ready",
        lambda *args, **kwargs: {"ready": True},
    )
    assert metal3_deploy.main(
        [
            "network",
            "--dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--apply",
            "--wait",
            "--target-kubeconfig",
            str(tmp_path / "target.kubeconfig"),
        ]
    ) == 0
    evidence = json.loads((tmp_path / metal3_deploy.NETWORK_FILE).read_text())
    assert evidence["status"] == "ready"
    assert evidence["readiness"]["ready"] is True


def test_network_apply_fails_when_an_enabled_plugin_has_no_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        metal3_deploy.network_stack_adapter,
        "apply_network_stack",
        lambda *args, **kwargs: {
            "apply_pending_plugins": ["calico"],
            "applied_plugins": [],
            "side_effects": False,
        },
    )
    assert metal3_deploy.main(["network", "--dir", str(tmp_path), "--apply"]) == 1
    evidence = json.loads((tmp_path / metal3_deploy.NETWORK_FILE).read_text())
    assert evidence["status"] == "blocked"
    assert evidence["apply"]["apply_pending_plugins"] == ["calico"]


def test_auto_lifecycle_runs_kubeconfig_network_then_verification(tmp_path, monkeypatch):
    calls = []

    def fake_deploy(arguments):
        calls.append(list(arguments))
        return 0

    settings = metal3_lab.LabSettings(
        hosts_dir=tmp_path,
        network_spec=tmp_path / "network.yaml",
        auto_network=True,
        target_ready_timeout=60,
        network_ready_timeout=60,
    )
    lab = metal3_lab.Metal3Lab(settings, deploy=fake_deploy)
    monkeypatch.setattr(lab, "apply_target", lambda: calls.append(["apply_target"]))
    monkeypatch.setattr(lab, "verify_target", lambda: calls.append(["verify_target"]))
    lab.apply_target()
    lab.configure_target_network()
    lab.verify_target()
    assert calls[0] == ["apply_target"]
    assert calls[1][0] == "target-kubeconfig"
    assert calls[2][0] == "network"
    assert "--apply" in calls[2] and "--wait" in calls[2]
    assert calls[3] == ["verify_target"]


def test_platform_stage_state_requires_trusted_pivot_worker_and_network_evidence():
    stages = metal3_deploy._platform_stage_state(
        {
            "plan_status": "validated_dry_run",
            "capi_pivot_source_preflight": {"preflight_validated": True},
            "dual_agent": {
                "pivot_completed": True,
                "target_agent_owns_cluster": True,
                "worker_provisioning_completed": True,
            },
        },
        local_network_ready=False,
    )
    assert stages == {
        "eph_live_os": True,
        "control_plane": True,
        "pivot": True,
        "workers": True,
        "network": False,
    }


def test_platform_stage_state_does_not_accept_local_plan_as_pivot_evidence():
    stages = metal3_deploy._platform_stage_state(
        {"plan_status": "validated_dry_run"}, local_network_ready=True
    )
    assert stages["pivot"] is False
    assert stages["workers"] is False
    assert stages["network"] is True


def test_auto_waits_for_platform_pivot_and_worker_evidence(tmp_path):
    results = iter([2, 0])
    calls = []

    def fake_deploy(arguments):
        calls.append(list(arguments))
        return next(results)

    lab = metal3_lab.Metal3Lab(
        metal3_lab.LabSettings(
            hosts_dir=tmp_path,
            deployment_id="ba002b95-6e72-4574-ad45-392555b3f9a3",
            retry_interval=0,
        ),
        deploy=fake_deploy,
        sleep=lambda _: None,
    )
    lab.wait_for_platform_deployment()
    assert len(calls) == 2
    assert calls[0][0] == "stages"
    assert "--require-ready" in calls[0]
