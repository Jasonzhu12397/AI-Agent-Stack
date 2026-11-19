"""The lab lifecycle is python orchestration, not a shell wrapper.

The only remaining vendored executables are host-bootstrap stages. Planning,
BMH/CAPI application, verification and cleanup ordering belong to the agent's
python pipeline, and host-mutating actions still require explicit consent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.eph_agent import metal3_lab


def settings(tmp_path, **overrides):
    values = {
        "vendor_dir": tmp_path / "vendor",
        "work_dir": tmp_path / "work",
        "hosts_dir": tmp_path / "work" / "hosts",
        "allow_host_mutation": True,
        "apply_retries": 2,
        "retry_interval": 0,
    }
    values.update(overrides)
    return metal3_lab.LabSettings(**values)


def test_preflight_accepts_a_sufficient_ubuntu_host():
    report = metal3_lab.preflight_host(
        os_release={"ID": "ubuntu", "VERSION_ID": "24.04"},
        cpus=8,
        memory_kib=16 * 1024 * 1024,
        command_exists=lambda name: True,
    )
    assert report.os_id == "ubuntu"
    assert report.os_version == "24.04"
    assert report.cpus == 8


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"os_release": {"ID": "centos"}}, "only on Ubuntu"),
        ({"cpus": 2}, "at least 4 CPUs"),
        ({"memory_kib": 1024}, "at least 8 GiB"),
        ({"command_exists": lambda name: name != "ip"}, "missing: ip"),
    ],
)
def test_preflight_fails_before_host_mutation(kwargs, message):
    values = {
        "os_release": {"ID": "ubuntu"},
        "cpus": 8,
        "memory_kib": 16 * 1024 * 1024,
        "command_exists": lambda name: True,
    }
    values.update(kwargs)
    with pytest.raises(metal3_lab.LabError, match=message):
        metal3_lab.preflight_host(**values)


def test_host_mutation_requires_consent(tmp_path):
    lab = metal3_lab.Metal3Lab(settings(tmp_path, allow_host_mutation=False))
    with pytest.raises(metal3_lab.LabError, match="allow-host-mutation"):
        lab.require_host_mutation_consent()


def test_prepare_orders_agent_and_host_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(
        metal3_lab,
        "preflight_host",
        lambda: metal3_lab.PreflightReport("ubuntu", "24.04", 8, 16 * 1024 * 1024),
    )
    calls = []
    host_stages = []

    def deploy(arguments):
        arguments = list(arguments)
        calls.append(arguments)
        if arguments[0] == "plan":
            out = Path(arguments[arguments.index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "plan.json").write_text(
                json.dumps({"assignments": [{"role": "control-plane"}]}), encoding="utf-8"
            )
        return 0

    def executor(stage, environment):
        host_stages.append(stage.name)
        assert environment["NODES_PLATFORM"] == "baremetal"

    lab = metal3_lab.Metal3Lab(settings(tmp_path), executor=executor, deploy=deploy)
    monkeypatch.setattr(lab, "verify_vendor", lambda: {})
    lab.prepare()

    assert host_stages == [name.removesuffix(".sh") for name in metal3_lab.HOST_BOOTSTRAP_SCRIPTS]
    assert [call[0] for call in calls] == ["plan", "apply", "verify"]
    # target application happens only after the management controllers exist
    assert host_stages[-1] == "03_launch_mgmt_cluster"
    assert not any("04_verify" in name or "make" in name for name in host_stages)


def test_plan_forwards_every_cluster_input(tmp_path):
    seen = []

    def deploy(arguments):
        seen.extend(arguments)
        out = tmp_path / "work" / "hosts"
        out.mkdir(parents=True, exist_ok=True)
        (out / "plan.json").write_text(json.dumps({"assignments": [{}]}), encoding="utf-8")
        return 0

    lab = metal3_lab.Metal3Lab(
        settings(
            tmp_path,
            project="project-1",
            control_plane_endpoint="10.0.0.50",
            image_url="https://images/eph-live.iso",
            k8s_version="v1.31.0",
            data_template=tmp_path / "data.yaml",
            write_back=True,
        ),
        deploy=deploy,
    )
    assert lab.plan() == 1
    for value in (
        "project-1",
        "10.0.0.50",
        "https://images/eph-live.iso",
        "v1.31.0",
        "--write-back",
    ):
        assert value in seen


def test_apply_retries_the_agent_without_a_shell_loop(tmp_path):
    attempts = []
    sleeps = []

    def deploy(arguments):
        attempts.append(list(arguments))
        return 1 if len(attempts) == 1 else 0

    lab = metal3_lab.Metal3Lab(
        settings(tmp_path, retry_interval=2), deploy=deploy, sleep=sleeps.append
    )
    lab.apply_target()
    assert len(attempts) == 2
    assert sleeps == [2]


def test_apply_failure_is_not_hidden(tmp_path):
    lab = metal3_lab.Metal3Lab(
        settings(tmp_path, apply_retries=2), deploy=lambda arguments: 1, sleep=lambda seconds: None
    )
    with pytest.raises(metal3_lab.LabError, match="could not deploy"):
        lab.apply_target()


def test_cleanup_uses_argv_and_never_calls_cleanup_scripts(tmp_path):
    lab = metal3_lab.Metal3Lab(settings(tmp_path, container_runtime="podman"))
    stages = lab.cleanup_stages()
    rendered = [" ".join(stage.argv) for stage in stages]

    assert any("kind delete cluster" in command for command in rendered)
    assert any("podman pod rm -f" in command for command in rendered)
    assert any("vm-setup/firewall.yml" in command for command in rendered)
    assert not any(command.endswith(".sh") for command in rendered)
    assert not any("make clean" in command for command in rendered)


def test_reference_import_does_not_overwrite_the_running_fork(tmp_path, monkeypatch):
    imported = []

    def import_tree(source, target):
        imported.append((source, target))

    monkeypatch.setattr(metal3_lab.vendor_import, "import_tree", import_tree)
    config = settings(tmp_path, vendor_source="/tmp/upstream.tar.gz")
    lab = metal3_lab.Metal3Lab(config)
    target = lab.reference()

    assert target == config.vendor_dir.with_name(config.vendor_dir.name + ".upstream")
    assert imported == [("/tmp/upstream.tar.gz", target)]
    assert target != config.vendor_dir


def test_invalid_control_plane_size_is_rejected(tmp_path):
    with pytest.raises(metal3_lab.LabError, match="control plane size"):
        metal3_lab.validate_settings(settings(tmp_path, control_plane_count=2))


def test_run_sh_is_gone_and_python_entry_point_exists():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "deploy" / "metal3-lab" / "run.sh").exists()
    assert (root / "tools" / "metal3_lab.py").is_file()
