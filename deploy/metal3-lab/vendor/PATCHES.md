# Vendored patches

Intentional divergences from upstream `metal3-io/metal3-dev-env` at commit
`7076f07624cdfba990f83bea24f749b37e3baad7`. Two kinds of divergence exist:

1. **Pruned paths** — upstream code that has no consumer in this repository is
   deleted after import. It is listed in `tools/vendor_import.py` (`PRUNE_PATHS`)
   and therefore does not show up as a file patch; `--check` reports it as
   `intentionally not vendored`.
2. **Patched files** — upstream files that stay but had to change. Each entry
   below records the file, the reason and how to verify it.

Local edits are allowed and expected; this file keeps them reviewable and
reviewable after `tools/metal3_lab.py reference`.

Format:

```
## <file>
- Reason: <why this repository must diverge>
- Change: <what changed, one sentence>
- Check: <command that proves the change is present>
```

## Pruned upstream code

Deleted by `tools/vendor_import.py` on every import, recorded in
`vendor/metal3-dev-env/VENDOR.json` under `pruned_paths`:

- `vm-setup/setup-playbook.yml`, `vm-setup/teardown-playbook.yml`,
  `vm-setup/library`, `vm-setup/roles/{libvirt,virtbmc,ovs,common}`
- `vbmc.sh`, `disable_apparmor_driver_libvirtd.sh`
- `PoCs/` (Kamaji PoC: libvirt domains, `sushy-tools`, shell-generated BMHs)
- `tests/`, `template/` (the VM-based e2e suite, now covered by the agent verify
  step plus this repository's pytest suite)
- `04_verify.sh`, `cluster_cleanup.sh`, `host_cleanup.sh`, `Makefile`
  (superseded by `apps/eph_agent/metal3_lab.py` and `metal3_readiness.py`)
- `tilt-setup/`, `Tiltfile`, `Tiltfile.md`, `hack/`, `ip_range_check.py`
- upstream repository metadata (`README.md`, `AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`, `OWNERS*`, `DCO`, `SECURITY_CONTACTS`, `renovate.json`,
  `.markdownlint-cli2.yaml`, `.yamllint.yaml`, `vars.md`)

Check: `python3 tools/vendor_import.py --check` prints one
`intentionally not vendored:` line and exits 0.

## Entries

### `01_prepare_host.sh`

- Reason: the lab always runs a kind management cluster; minikube needs libvirt
  and tilt was an upstream live-reload workflow, so neither provider is installed.
- Change: the minikube/kvm2 download block and the tilt install are gone; only
  kind is installed, and `lib/image_prepull.sh` runs unconditionally.
- Check: `grep -n "download_and_install_kind" deploy/metal3-lab/vendor/metal3-dev-env/01_prepare_host.sh`

### `02_configure_host.sh`

- Reason: physical machines expose their own BMC, so there is no libvirt to
  configure: no domain, no storage pool, no `virt_power` key, no vbmc/sushy-tools
  container, no KVM module tuning.
- Change: the libvirt group add, the `tdp_mmu` workaround, the minikube VM
  helpers, the `virt_power` key, the `setup-playbook.yml` run, the `virsh`
  storage-pool block, the `virsh` network restart and the emulator containers are
  removed; the Ubuntu branch only sets up the bridges with `ip`/`brctl` before
  applying the firewall role.
- Check: `grep -n "Skipping libvirt\|virsh\|setup-playbook" deploy/metal3-lab/vendor/metal3-dev-env/02_configure_host.sh` prints nothing but the emulator cleanup line.

### `03_launch_mgmt_cluster.sh`

- Reason: BMHs and target CAPI resources are planned and applied by the Python
  agent, not by a management-bootstrap script.
- Change: the libvirt/fake inventory renderers, shell BMH apply path, local
  operator loops and the Python-wrapper BMH function are all removed. The script
  ends after BMO/Ironic/CAPI roll out; `Metal3Lab.prepare()` then calls
  `metal3_deploy.py apply` and `verify` directly.
- Check: `grep -n "apply_bm_hosts\|metal3_deploy\|make-bm-worker" deploy/metal3-lab/vendor/metal3-dev-env/03_launch_mgmt_cluster.sh` prints nothing.

### `vm-setup/roles/firewall`

- Reason: the rules opened the virtual BMC emulator ports and moved interfaces
  into a `libvirt` zone that does not exist on a host running no VMs.
- Change: the vbmc/sushy port rules and their defaults are removed and every
  rule uses the host's `public` zone.
- Check: `grep -rn "vbmc\|sushy\|libvirt" deploy/metal3-lab/vendor/metal3-dev-env/vm-setup/roles/firewall` prints nothing.

### `vm-setup/roles/packages_installation`

- Reason: the host does not run virtual machines, so hypervisor, firmware and
  libvirt client packages are pure attack surface and install time.
- Change: 31 package entries removed (libvirt*, qemu-kvm/qemu-img, virt-install,
  ovmf/edk2-ovmf, libguestfs-tools, virtualbmc, libvirt python bindings and the
  polkit agents that only existed for libvirt access).
- Check: `grep -rn "libvirt\|qemu\|virt-install\|virtualbmc" deploy/metal3-lab/vendor/metal3-dev-env/vm-setup/roles/packages_installation` prints nothing.

### `config_example.sh`

- Reason: the defaults must not advertise images and platforms this lab cannot
  run.
- Change: `VBMC_IMAGE`, `SUSHY_TOOLS_IMAGE`, `NODES_PLATFORM=fake`,
  `FAKE_IPA_IMAGE` and `SKIP_APPLY_BMH` blocks removed; `BOOTSTRAP_CLUSTER` is
  documented as `kind`.
- Check: `grep -n "vbmc\|sushy\|minikube\|fake" deploy/metal3-lab/vendor/metal3-dev-env/config_example.sh` prints nothing.

### `ubuntu_bridge_network_configuration.sh`

- Reason: there is no libvirt network named `external` to restart.
- Change: the trailing `virsh net-destroy`/`net-start` block is removed; bridge
  creation with `brctl`/`ip` is unchanged.
- Check: `grep -n "virsh" deploy/metal3-lab/vendor/metal3-dev-env/ubuntu_bridge_network_configuration.sh` prints nothing.

### `vm-setup/requirements.yml`

- Reason: the `ovs` role that needed the Open vSwitch collection is pruned.
- Change: the `openvswitch.openvswitch` collection is removed from the list.
- Check: `grep -n "openvswitch" deploy/metal3-lab/vendor/metal3-dev-env/vm-setup/requirements.yml` prints nothing.

Reference note: `tools/metal3_lab.py reference` imports upstream into a sibling
`.upstream` directory and never overwrites this fork. After deliberate edits,
run `tools/metal3_lab.py reseal` to record the new baseline.
