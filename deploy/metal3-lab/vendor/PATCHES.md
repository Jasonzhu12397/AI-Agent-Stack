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
re-appliable after `run.sh restore`.

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

- Reason: BMH manifests are planned from the platform database and applied by
  the agent; upstream generated them with `go run make-bm-worker` from
  `ironic_nodes.json` and applied them with kubectl, which is exactly the work
  `tools/metal3_deploy.py` now owns.
- Change: `make_bm_hosts()`, `apply_bm_hosts()`, `launch_fake_ipa()`, the
  minikube/tilt branches and the local-operator loops are removed;
  `apply_bm_hosts_from_dir()` is the only BMH path and runs
  `python3 tools/metal3_deploy.py apply --dir "$BARE_METAL_HOSTS_DIR"` with
  retries. Launching BMO/Ironic/CAPI, the registry and kind are unchanged.
- Check: `grep -n "apply_bm_hosts_from_dir\|make-bm-worker" deploy/metal3-lab/vendor/metal3-dev-env/03_launch_mgmt_cluster.sh` (second pattern found only in a comment).

### `04_verify.sh`

- Reason: there is no virtual machine and no BMC emulator to expect on bare
  metal, and no judgement about the cluster belongs in shell: matching text out
  of the cluster CLI turns a missing field into an empty string and a pass.
- Change: `check_bm_hosts()`, `check_k8s_entity()`, `check_k8s_rs()`,
  `check_container()`, the CRD grep loop, the bridge loop and the Ironic curl
  probe are all removed. The script only calls `check_bmhs_baremetal()`, which
  runs `tools/metal3_deploy.py verify`; that asks the agent's
  `observe_baremetalhosts` about the hosts and
  `apps/eph_agent/metal3_readiness.py` about CRDs, controllers, replica sets,
  host bridges, infra containers and Ironic authentication.
- Check: `grep -n "kubectl\|jq\|virsh" deploy/metal3-lab/vendor/metal3-dev-env/04_verify.sh` prints nothing.

### `Makefile`

- Reason: the VM lifecycle and lint targets invoked code that is no longer
  vendored.
- Change: `test`, `provision`, `pivot`, `repivot`, `deprovision`,
  `verify_provision` and `lint` targets removed; `install/configure/launch/verify/clean/prepull_images` kept.
- Check: `grep -c "make test\|shellcheck" deploy/metal3-lab/vendor/metal3-dev-env/Makefile` returns 0.

### `lib/common.sh`

- Reason: shared defaults still described the virtual lab.
- Change: `NODES_PLATFORM` defaults to `baremetal`, `BOOTSTRAP_CLUSTER` defaults
  to `kind`, and `list_nodes()`, `manage_libvirtd()`,
  `get_disableCertificateVerification_from_verify_ca()`, the `NODES_FILE` /
  `FAKE_NODES_FILE` paths, `LIBVIRT_DOMAIN_TYPE` / `LIBVIRT_DEFAULT_URI`,
  `BMO_RUN_LOCAL` / `CAPM3_RUN_LOCAL` and the emulator image variables are
  removed; `remove_ironic_containers` no longer sweeps vbmc/sushy-tools.
- Check: `grep -n "list_nodes\|manage_libvirtd\|LIBVIRT" deploy/metal3-lab/vendor/metal3-dev-env/lib/common.sh` prints nothing.

### `lib/download.sh`

- Reason: minikube and its kvm2 driver are no longer providers.
- Change: `download_and_install_minikube`, `download_and_install_kvm2_driver`
  removed, together with their calls in the bulk installer.
- Check: `grep -n "minikube" deploy/metal3-lab/vendor/metal3-dev-env/lib/download.sh` prints nothing.

### `lib/network.sh`

- Reason: the minikube IPv6 bridge address only existed for a minikube VM.
- Change: the `MINIKUBE_BMNET_V6_IP` block is removed.
- Check: `grep -n "MINIKUBE" deploy/metal3-lab/vendor/metal3-dev-env/lib/network.sh` prints nothing.

### `cluster_cleanup.sh`

- Reason: only kind is ever created.
- Change: the tilt branch and the minikube deletion are removed; kind deletion
  and the local ironic container removal remain.
- Check: `grep -n "minikube\|tilt" deploy/metal3-lab/vendor/metal3-dev-env/cluster_cleanup.sh` prints nothing.

### `host_cleanup.sh`

- Reason: there is no fake-ipa container, no locally running operator and no
  `teardown-playbook.yml` to replay.
- Change: the fake-ipa removal, the `BMO_RUN_LOCAL`/`CAPM3_RUN_LOCAL` kill
  blocks, the teardown playbook run and the libvirt serial-log/BMH-artifact
  cleanup are removed; bridge teardown and firewall removal remain.
- Check: `grep -n "teardown-playbook\|fake-ipa\|run-bmo-loop" deploy/metal3-lab/vendor/metal3-dev-env/host_cleanup.sh` prints nothing.

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

Restore note: `run.sh restore` re-imports the upstream tree, applies
`PRUNE_PATHS` again and therefore erases these file patches. Re-apply them (they
are small, branch-scoped hunks) or keep the patched tree under version control.
