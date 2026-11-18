import pytest

from apps.target_agent.ntp_attestation import (
    NtpAttestationError,
    verify_control_plane_ntp_bootstrap,
)


def _machine_and_config(index, approved_source, worker_network, install_command=None):
    machine_name = f"control-plane-{index}"
    machine_uid = f"machine-uid-{index}"
    config_name = f"control-plane-{index}-bootstrap"
    script = "\n".join((
        install_command or "apt-get install -y chrony iproute2",
        "server " + approved_source + " iburst",
        "allow " + worker_network,
        "chronyc -n sources",
        "chronyc tracking",
        "ss -lun",
        "systemctl enable chronyd",
        "systemctl restart chronyd",
    ))
    machine = {
        "metadata": {
            "name": machine_name,
            "uid": machine_uid,
            "labels": {
                "cluster.x-k8s.io/control-plane": "",
                "cluster.x-k8s.io/cluster-name": "target-cluster",
            },
        },
        "spec": {
            "bootstrap": {
                "configRef": {"kind": "KubeadmConfig", "name": config_name}
            }
        },
        "status": {
            "bootstrapReady": True,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    config = {
        "metadata": {
            "name": config_name,
            "ownerReferences": [{
                "kind": "Machine",
                "name": machine_name,
                "uid": machine_uid,
                "controller": True,
            }],
        },
        "spec": {"preKubeadmCommands": [script]},
        "status": {"ready": True},
    }
    return machine, config


def test_control_plane_ntp_attestation_requires_all_master_bootstrap_scripts():
    machines = []
    configs = []
    for index in range(1, 4):
        machine, config = _machine_and_config(
            index,
            "192.0.2.10",
            "10.130.60.80/28",
        )
        machines.append(machine)
        configs.append(config)
    result = verify_control_plane_ntp_bootstrap(
        {"items": machines},
        {"items": configs},
        ["192.0.2.10"],
        ["10.130.60.80/28"],
        "target-cluster",
    )
    assert result["target_control_plane_ntp_profile_verified"] is True
    assert result["target_control_plane_ntp_bootstrap_sync_gate_verified"] is True
    assert result["verified_master_count"] == 3


def test_control_plane_ntp_attestation_rejects_external_fallback_pool():
    machines = []
    configs = []
    for index in range(1, 4):
        machine, config = _machine_and_config(
            index,
            "192.0.2.10",
            "10.130.60.80/28",
        )
        if index == 1:
            config["spec"]["preKubeadmCommands"][0] += "\npool pool.ntp.org iburst"
        machines.append(machine)
        configs.append(config)
    with pytest.raises(NtpAttestationError, match="EXTERNAL_FALLBACK_FOUND"):
        verify_control_plane_ntp_bootstrap(
            {"items": machines},
            {"items": configs},
            ["192.0.2.10"],
            ["10.130.60.80/28"],
            "target-cluster",
        )



def test_control_plane_ntp_attestation_rejects_unapproved_configured_source():
    machines = []
    configs = []
    for index in range(1, 4):
        machine, config = _machine_and_config(
            index,
            "pool.ntp.org",
            "10.130.60.80/28",
        )
        machines.append(machine)
        configs.append(config)
    with pytest.raises(NtpAttestationError, match="NOT_APPROVED"):
        verify_control_plane_ntp_bootstrap(
            {"items": machines},
            {"items": configs},
            ["192.0.2.10"],
            ["10.130.60.80/28"],
            "target-cluster",
        )

@pytest.mark.parametrize(
    "install_command",
    [
        "apt-get install -y chrony iproute2",
        "dnf install -y chrony iproute",
        "yum install -y chrony iproute",
        "zypper --non-interactive install chrony iproute2",
    ],
)
def test_control_plane_ntp_attestation_accepts_supported_chrony_package_managers(
    install_command,
):
    machines = []
    configs = []
    for index in range(1, 4):
        machine, config = _machine_and_config(
            index,
            "192.0.2.10",
            "10.130.60.80/28",
            install_command=install_command,
        )
        machines.append(machine)
        configs.append(config)
    result = verify_control_plane_ntp_bootstrap(
        {"items": machines},
        {"items": configs},
        ["192.0.2.10"],
        ["10.130.60.80/28"],
        "target-cluster",
    )
    assert result["target_control_plane_ntp_bootstrap_sync_gate_verified"] is True


def test_control_plane_ntp_attestation_rejects_chrony_without_package_install():
    machines = []
    configs = []
    for index in range(1, 4):
        machine, config = _machine_and_config(
            index,
            "192.0.2.10",
            "10.130.60.80/28",
            install_command="echo chrony is expected to be preinstalled",
        )
        machines.append(machine)
        configs.append(config)
    with pytest.raises(NtpAttestationError, match="BOOTSTRAP_VERIFICATION_MISSING"):
        verify_control_plane_ntp_bootstrap(
            {"items": machines},
            {"items": configs},
            ["192.0.2.10"],
            ["10.130.60.80/28"],
            "target-cluster",
        )
