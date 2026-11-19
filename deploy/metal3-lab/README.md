# Metal3 Lab (physical machines)

This runner executes **vendored** Metal3 dev-env source that lives in this
repository under `deploy/metal3-lab/vendor/metal3-dev-env/`. The tree was imported
from `metal3-io/metal3-dev-env` at commit
`7076f07624cdfba990f83bea24f749b37e3baad7` and is pinned by
`vendor/metal3-dev-env/VENDOR.json` plus a per-file `MANIFEST.sha256`.

There is no git dependency at run time: no remote clone, no `fetch`, no
`checkout`, no submodule, no tracking branch. The code is ordinary repository
files, so it can be edited, reviewed, patched and re-developed locally — or by an
agent — exactly like the rest of this project.

## Two platforms, one code base

`METAL3_LAB_PLATFORM` decides which half of the vendored tree runs:

| Platform | What happens | Who it is for |
|---|---|---|
| `baremetal` (default) | BMH manifests are rendered from your inventory and applied. No libvirt, no `vbmc`, no `sushy-tools`, no VM, no `/dev/kvm`. | Real hardware behind a real BMC (Redfish/IPMI). |
| `libvirt` | Upstream demo: the `vm-setup` roles create virtual bare metal hosts on this host, with `vbmc`/`sushy-tools` emulating their BMCs. | Demos, CI, a laptop without hardware. |

The bare metal path is not a fork of the workflow — it is the same `01/02/03/04`
chain with three deliberate, recorded patches (see `vendor/PATCHES.md`):

- `02_configure_host.sh` skips the `vm-setup/setup-playbook.yml` run (libvirt +
  virtbmc roles) and does not start `vbmc`/`sushy-tools`; the provisioning bridge
  is attached to your real NIC instead of a veth pair.
- `03_launch_mgmt_cluster.sh` never generates BMHs from a libvirt inventory; it
  applies the manifests you rendered from your own machine list.
- `04_verify.sh` has no `virsh` steps and no BMC emulator containers; it verifies
  the BMHs themselves (status `OK`, provisioning state `ready`/`available`).

## Bare metal workflow

1. Describe your machines once:

   ```bash
   cp deploy/metal3-lab/hosts/baremetal-nodes.example.yaml \
      deploy/metal3-lab/hosts/baremetal-nodes.yaml
   $EDITOR deploy/metal3-lab/hosts/baremetal-nodes.yaml
   ```

   Each entry needs `name`, `bmc_address`, `username`, `password`, `boot_mac`
   and optionally `boot_mode`, `verify_bmc_ca`, `online`, `root_device_hints`,
   `labels`. The BMC password lives in a Secret rendered next to the BMH; the
   filled-in inventory should stay out of version control.

2. Render and apply:

   ```bash
   export METAL3_LAB_ALLOW_HOST_MUTATION=1
   export METAL3_LAB_WORK_DIR=/var/lib/metal3-lab
   bash deploy/metal3-lab/run.sh prepare
   ```

   `prepare` renders `.metal3-lab/hosts/bmhs.yaml` (also one file per machine),
   exports `BARE_METAL_HOSTS_DIR`, then runs the vendored `01/02/03/04`.

3. Inspect, then re-verify at any time:

   ```bash
   bash deploy/metal3-lab/run.sh verify
   ```

The renderer can also be used on its own:

```bash
python3 tools/bmh_from_inventory.py --inventory hosts.yaml --out-dir hosts
python3 tools/bmh_from_inventory.py --inventory hosts.yaml --count
```

It rejects duplicate names, duplicate boot MACs, non-DNS-label names and
malformed MACs or BMC schemes, and only ever deletes the files it wrote itself.

## Actions

| Action | Vendored code |
|---|---|
| `prepare` | render BMHs, then `01_prepare_host.sh` -> `02_configure_host.sh` -> `03_launch_mgmt_cluster.sh` -> `04_verify.sh` |
| `verify` | `04_verify.sh` |
| `lifecycle` | `make test` (provision, pivot, re-provision, deprovision) |
| `clean` | `make clean` |
| `restore` | re-import the pinned tree from its archive, no git |

## Safety and host requirements

The scripts modify host networking, packages, firewall rules and cluster state.
Point them at a host you are willing to reconfigure. `prepare`, `lifecycle` and
`clean` require `METAL3_LAB_ALLOW_HOST_MUTATION=1`.

Minimum documented size is 4 CPUs and 8 GiB RAM for the Ubuntu 22.04/24.04
management host. In `baremetal` mode KVM is irrelevant and is not required; in
`libvirt` mode KVM (or explicitly selected QEMU, which upstream documents as
experimental) is required.

The runner records timestamped logs under `$METAL3_LAB_WORK_DIR/artifacts`,
verifies the vendored tree before every action, and logs a warning when the
vendored code contains local edits.

## Editing the vendored code

1. Edit files under `deploy/metal3-lab/vendor/metal3-dev-env/` directly.
2. `python3 tools/vendor_import.py --check` reports edited files as `local change:`.
3. Record intentional divergences in `vendor/PATCHES.md`.
4. `run.sh restore` re-imports the pinned upstream tree and replaces local edits —
   re-apply your patches afterwards.

See `vendor/README.md` for the full layout and the import rules.

## What this validates

The lifecycle validates virtualized CAPI/Metal3 provisioning and pivot
behaviour and, on the bare metal path, real BMH registration and introspection.
It is a provider/environment validation tier. This is not an AI Agent Stack three-agent end-to-end test. It does not claim to have exercised
Cloud Control Agent command scheduling, EPH Agent pivot execution, Target Agent
handoff, the project's worker provisioning adapter, or the project's EPH cleanup.

The repository's own agent integration must be layered onto this live environment:
observe real Cluster/Machine/Metal3Machine/BMH objects, invoke the agents through
the Cloud Control API and verify state transitions and durable attestations.
Unit and mocked API tests must never be reported as `LIBVIRT_IRONIC` evidence.

## Upstream references

- [Metal3 dev-env](https://github.com/metal3-io/metal3-dev-env)
- [Developer environment guide](https://book.metal3.io/developer_environment/tryit)
- [Metal3 quick-start](https://book.metal3.io/quick-start)
- [BMO E2E guide](https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md)
