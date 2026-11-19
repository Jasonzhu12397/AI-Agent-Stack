"""The Metal3 lab targets physical machines, not libvirt VMs.

These tests pin the bare metal contract:

* the inventory -> BMH renderer produces Secrets plus BareMetalHost objects and
  rejects anything ambiguous,
* the runner defaults to the baremetal platform and does not demand /dev/kvm,
* the vendored scripts are patched so that no VM, no virtual BMC emulator and
  no virsh call is used in that mode - and the libvirt demo path stays intact.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "metal3-lab" / "run.sh"
VENDOR = ROOT / "deploy" / "metal3-lab" / "vendor" / "metal3-dev-env"
EXAMPLE_INVENTORY = ROOT / "deploy" / "metal3-lab" / "hosts" / "baremetal-nodes.example.yaml"

sys.path.insert(0, str(ROOT))
import tools.bmh_from_inventory as bmh  # noqa: E402


def write_inventory(path: Path, machines: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"machines": machines}, sort_keys=False), encoding="utf-8")
    return path


def machine(**overrides) -> dict:
    base = {
        "name": "bm-node-1",
        "bmc_address": "redfish+https://10.0.0.11/redfish/v1/Systems/1",
        "username": "admin",
        "password": "secret",
        "boot_mac": "aa:bb:cc:dd:ee:01",
    }
    base.update(overrides)
    return base


def test_example_inventory_renders_secrets_and_baremetalhosts(tmp_path):
    machines = bmh.load_inventory(EXAMPLE_INVENTORY)

    assert [item.name for item in machines] == ["bm-node-1", "bm-node-2"]
    documents = bmh.render_documents(machines)
    kinds = [document["kind"] for document in documents]
    assert kinds == ["Secret", "BareMetalHost", "Secret", "BareMetalHost"]

    host = documents[1]
    assert host["apiVersion"] == "metal3.io/v1alpha1"
    assert host["spec"]["bmc"]["address"] == "redfish+https://10.0.0.11/redfish/v1/Systems/1"
    assert host["spec"]["bmc"]["credentialsName"] == "bm-node-1-bmc"
    assert host["spec"]["bootMACAddress"] == "aa:bb:cc:dd:ee:01"
    assert host["spec"]["bootMode"] == "UEFI"
    assert host["spec"]["bmc"]["disableCertificateVerification"] is True
    assert documents[0]["stringData"] == {"username": "admin", "password": "CHANGE-ME"}


def test_mac_and_address_normalisation():
    assert bmh.normalise_mac("AA-BB-CC-DD-EE-0F") == "aa:bb:cc:dd:ee:0f"
    assert bmh.normalise_mac("aabbccddee0f") == "aa:bb:cc:dd:ee:0f"
    assert bmh.normalise_bmc_address("10.0.0.11/redfish/v1/Systems/1").startswith("redfish+https")
    assert bmh.normalise_bmc_address("ipmi://10.0.0.12") == "ipmi://10.0.0.12"

    with pytest.raises(bmh.InventoryError):
        bmh.normalise_mac("aa:bb:cc:dd:ee")
    with pytest.raises(bmh.InventoryError):
        bmh.normalise_bmc_address("redfisch+https://bmc")


def test_inventory_without_machines_is_rejected(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("machines: []\n", encoding="utf-8")

    with pytest.raises(bmh.InventoryError, match="non-empty list"):
        bmh.load_inventory(path)


def test_duplicate_names_and_macs_are_rejected(tmp_path):
    duplicated_name = write_inventory(
        tmp_path / "names.yaml",
        [machine(), machine(boot_mac="aa:bb:cc:dd:ee:02")],
    )
    with pytest.raises(bmh.InventoryError, match="duplicate machine name"):
        bmh.load_inventory(duplicated_name)

    duplicated_mac = write_inventory(
        tmp_path / "macs.yaml",
        [machine(), machine(name="bm-node-2")],
    )
    with pytest.raises(bmh.InventoryError, match="duplicate boot_mac"):
        bmh.load_inventory(duplicated_mac)


def test_invalid_machine_name_is_rejected(tmp_path):
    path = write_inventory(tmp_path / "bad-name.yaml", [machine(name="BM_Node_1")])

    with pytest.raises(bmh.InventoryError, match="not a valid DNS label"):
        bmh.load_inventory(path)


def test_renderer_writes_combined_and_per_machine_files(tmp_path):
    machines = bmh.load_inventory(EXAMPLE_INVENTORY)
    combined, directory = bmh.write_manifests(machines, tmp_path / "hosts")

    assert combined.name == "bmhs.yaml"
    assert (directory / "bm-node-1-bmh.yaml").is_file()
    assert (directory / "bm-node-2-bmh.yaml").is_file()
    assert len(list(yaml.safe_load_all(combined.read_text(encoding="utf-8")))) == 4


def test_renderer_leaves_foreign_files_alone(tmp_path):
    out = tmp_path / "hosts"
    out.mkdir()
    foreign = out / "operator-notes.yaml"
    foreign.write_text("keep: me\n", encoding="utf-8")

    bmh.write_manifests(bmh.load_inventory(EXAMPLE_INVENTORY), out)

    assert foreign.exists()


def test_renderer_cli_count_and_output(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "bmh_from_inventory.py"),
            "--inventory",
            str(EXAMPLE_INVENTORY),
            "--count",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "2"

    out = tmp_path / "out"
    rendered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "bmh_from_inventory.py"),
            "--inventory",
            str(EXAMPLE_INVENTORY),
            "--out-dir",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "rendered 2 BareMetalHost manifest(s)" in rendered.stdout
    assert (out / "bmhs.yaml").is_file()


def test_runner_has_no_platform_switch_and_no_kvm_gate():
    source = RUNNER.read_text(encoding="utf-8")
    assert "METAL3_LAB_PLATFORM" not in source
    assert "NODES_PLATFORM" not in source
    assert "/dev/kvm" in source  # mentioned only to say it is unused
    assert "KVM is irrelevant" in source or "never starts a virtual machine" in source
    assert "BARE_METAL_HOSTS_DIR" in source
    assert "tools/metal3_deploy.py" in source


def test_vendored_scripts_carry_no_virtualisation_path():
    configure = (VENDOR / "02_configure_host.sh").read_text(encoding="utf-8")
    launch = (VENDOR / "03_launch_mgmt_cluster.sh").read_text(encoding="utf-8")
    verify = (VENDOR / "04_verify.sh").read_text(encoding="utf-8")

    # no libvirt, no virtual BMC emulator, no VM creation playbook
    assert "Skipping libvirt" not in configure
    assert "setup-playbook.yml" not in configure
    assert "virsh" not in configure
    assert "configure_minikube" not in configure

    # BMHs are never generated in shell any more: the agent applies the plan
    assert "apply_bm_hosts_from_dir" in launch
    assert "BARE_METAL_HOSTS_DIR" in launch
    assert "make-bm-worker" not in launch
    assert "make_bm_hosts()" not in launch
    assert "list_nodes" not in launch

    assert "check_bmhs_baremetal" in verify
    assert "check_bm_hosts(" not in verify
    assert "virsh" not in verify
    assert 'EXPTD_CONTAINERS="httpd-infra registry"' in verify


def test_baremetal_documents_have_no_vm_creation_path():
    docs = (ROOT / "deploy" / "metal3-lab" / "README.md").read_text(encoding="utf-8")
    assert "physical machines" in docs
    assert "not vendored" in docs
    assert "PRUNE_PATHS" in docs
    assert "bmh_from_inventory" in docs
