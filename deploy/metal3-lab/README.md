# Metal3 Virtual Lab

This runner executes **vendored** Metal3 dev-env source that lives in this
repository under `deploy/metal3-lab/vendor/metal3-dev-env/`. The tree was imported
from `metal3-io/metal3-dev-env` at commit
`7076f07624cdfba990f83bea24f749b37e3baad7` and is pinned by
`vendor/metal3-dev-env/VENDOR.json` plus a per-file `MANIFEST.sha256`.

There is no git dependency at run time: no remote clone, no `fetch`, no
`checkout`, no submodule, no tracking branch. The code is ordinary repository
files, so it can be edited, reviewed, patched and re-developed locally — or by an
agent — exactly like the rest of this project.

## Vendored workflow

`run.sh` maps four stable commands onto the vendored entry points:

| Action | Vendored code |
|---|---|
| `prepare` | `01_prepare_host.sh` -> `02_configure_host.sh` -> `03_launch_mgmt_cluster.sh` -> `04_verify.sh` |
| `verify` | `04_verify.sh` |
| `lifecycle` | `make test` (provision, pivot, re-provision, deprovision) |
| `clean` | `make clean` |
| `restore` | re-import the pinned tree from its archive, no git |

The upstream workflow creates a Kubernetes management cluster and virtual
bare-metal hosts, so the scripts are host-mutating by nature.

## Safety and host requirements

The scripts modify host networking, packages, libvirt and cluster state. Run them
only inside a disposable Ubuntu 22.04/24.04 VM. Do not run `prepare`, `lifecycle`
or `clean` on a production host.

Minimum documented development host size for Ubuntu targets is 4 CPUs and 8 GiB
RAM. KVM/nested virtualization is the normal hypervisor. Upstream QEMU mode is
experimental and must be selected explicitly.

## Run

```bash
export METAL3_LAB_ALLOW_HOST_MUTATION=1
export METAL3_LAB_WORK_DIR=/var/lib/metal3-lab
bash deploy/metal3-lab/run.sh prepare
bash deploy/metal3-lab/run.sh verify
bash deploy/metal3-lab/run.sh lifecycle
```

After collecting logs and confirming that no test artifacts are needed:

```bash
bash deploy/metal3-lab/run.sh clean
```

The runner records timestamped logs under `$METAL3_LAB_WORK_DIR/artifacts`,
verifies the vendored tree before every action, and logs a warning when the
vendored code contains local edits.

## Editing the vendored code

1. Edit files under `deploy/metal3-lab/vendor/metal3-dev-env/` directly.
2. `python3 tools/vendor_import.py --check` reports edited files as `local change:`.
3. Record intentional divergences in `vendor/PATCHES.md`.
4. `run.sh restore` re-imports the pinned upstream tree and replaces local edits.

See `vendor/README.md` for the full layout and the import rules.

## What this validates

The dev-env lifecycle validates virtualized CAPI/Metal3 provisioning and pivot
behaviour. It is a provider/environment validation tier. This is not an AI Agent Stack three-agent end-to-end test. It does not claim to have exercised
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
