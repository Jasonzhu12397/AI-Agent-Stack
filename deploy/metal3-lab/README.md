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

## One platform: physical machines

This lab provisions real hardware behind real BMCs (Redfish/IPMI). There is no
platform switch, no virtual machine, no `/dev/kvm` requirement: the virtual half
of upstream dev-env is not merely disabled, it is **not vendored**.

`tools/vendor_import.py` imports the pinned upstream tree and then deletes the
paths that no longer have a consumer here (`PRUNE_PATHS` in that file):

| Removed | Why it cannot stay |
|---|---|
| `vm-setup/roles/libvirt`, `virtbmc`, `ovs`, `setup-playbook.yml`, `teardown-playbook.yml`, `vbmc.sh`, `disable_apparmor_driver_libvirtd.sh` | create virtual bare metal hosts and emulate their BMCs |
| `PoCs/` | a Kamaji demo built on libvirt domains, `sushy-tools` and shell-generated BMHs |
| `tests/`, `template/` | the upstream VM-based e2e suite, replaced by the agent's verify step and this repository's pytest suite |
| `hack/`, `tilt-setup/`, `Tiltfile*`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `OWNERS*`, `DCO`, `SECURITY_CONTACTS`, `renovate.json`, `.markdownlint*`, `.yamllint.yaml`, `README.md`, `vars.md` | upstream developer tooling, CI and repository metadata |

What remains is the runtime skeleton the runner actually executes, and four
deliberate, recorded edits (see `vendor/PATCHES.md`):

- `02_configure_host.sh` creates the provisioning/external bridges with `ip` and
  `brctl` on your real NICs and starts no BMC emulator.
- `03_launch_mgmt_cluster.sh` generates no BMH from an inventory file and runs no
  `go run make-bm-worker`: it hands the manifests the agent planned to
  `tools/metal3_deploy.py`.
- `04_verify.sh` has no `virsh` steps and no emulator containers; the agent judges
  the BMHs themselves (status `OK`, provisioning state `ready`/`available`).
- `01_prepare_host.sh`/`Makefile`/`lib/*` only install and drive what is left.

## Python owns the pipeline, the agent owns the decisions

Nothing in this path parses cluster data in shell. `run.sh` exports the inputs
and `tools/metal3_deploy.py` does the rest:

| Stage | Code |
|---|---|
| Read the fleet | `tools/metal3_fleet_source.py` -> `AgentMachine` rows (BMC endpoint, credential reference, reported CPU/RAM/disk/NICs) |
| Reason about roles | `apps/eph_agent/fleet_planning.py`: explicit `ai-agent-stack.io/role` labels win, otherwise machines are ranked by reported capacity and identity status |
| Render BMHs | `apps/eph_agent/capi_renderer.render_bmh_manifests` (the EPH agent's own template) |
| Render the target cluster | `apps.eph_agent.capi_renderer.render_capi_metal3_manifests` with the control plane count and the worker pools the plan produced |
| Apply | `apps/eph_agent/metal3_skill.apply_baremetalhosts` then `apply_capi_cluster_resources` |
| Verify | `apps/eph_agent/metal3_skill.observe_baremetalhosts` |

`plan.json` records every assignment with the sentence that justified it and the
facts the plan had to do without, so a master that was chosen on weak data says
so out loud.

`METAL3_LAB_WRITE_BACK=1` (or `--write-back`) mirrors the plan into the
platform: `AgentMachine.bmh_name` and `lifecycle_state` are set to what the
planner decided and one `fleet.machine.role_planned` audit event is recorded per
machine with the reason, score and control-plane size. The platform therefore
never keeps a second, stale opinion about who is a master.

How the layers stack:

1. The EPH live-OS single-node cluster runs Cluster API and Metal3.
2. `plan`/`apply` deploy the target cluster's **control plane** masters.
3. The target agent then pivots the Metal3 data (`execute_capi_pivot`) and
   provisions the **worker** machines - the data plane
   (`execute_worker_provisioning`).
4. The network stack adds the CNI and the data plane plugins
   (`apps/target_agent/network_plugins.plan_network_stack`).

The lab CLI covers the first two stages; the pivot and worker stages are agent
commands driven by the platform, not shell. `metal3_deploy.py stages` reports
where the target cluster stands, and `metal3_deploy.py network` plans the CNI
plus the data plane plugins through `plan_network_stack`:

```bash
python3 tools/metal3_deploy.py stages  --dir .metal3-lab/hosts
python3 tools/metal3_deploy.py network --dir .metal3-lab/hosts --spec network.json
```

## Bare metal workflow

The machine list is generated data, not a file you maintain by hand: the
platform database already holds each machine's BMC endpoint, the vault
reference for its credentials and the NICs the agent reported.

1. Register the machine in the platform (BMC endpoint, credential reference,
   hardware inventory report). `AgentMachine` is the single source of truth.

2. Render and apply:

   ```bash
   export METAL3_LAB_ALLOW_HOST_MUTATION=1
   export METAL3_LAB_WORK_DIR=/var/lib/metal3-lab
   export METAL3_LAB_PROJECT_ID=<platform project id>   # optional scope
   bash deploy/metal3-lab/run.sh prepare
   ```

   `prepare` reads `AgentMachine` rows, resolves the BMC credentials through
   `BMC_CREDENTIAL_RESOLVER`, derives the boot MAC from the reported inventory
   and writes `.metal3-lab/hosts/bmhs.yaml` (plus one file per machine). Machines
   missing a BMC endpoint, a credential reference or an unambiguous boot NIC are
   reported and skipped, never guessed. The bundle is then handed to the agent.

3. Inspect, then re-verify at any time:

   ```bash
   bash deploy/metal3-lab/run.sh verify
   ```

The renderer can also be used on its own:

```bash
python3 tools/bmh_from_inventory.py --from-platform --project <id> --out-dir hosts
python3 tools/bmh_from_inventory.py --from-platform --count
```

Offline, without a database, a static inventory is still supported:

```bash
METAL3_LAB_INVENTORY_SOURCE=file \
python3 tools/bmh_from_inventory.py --inventory hosts.yaml --out-dir hosts
```

Both sources go through the same validation: duplicate names, duplicate boot
MACs, non-DNS-label names and malformed MACs or BMC schemes are rejected, and
only the files this tool wrote are ever deleted.

Applying and verifying that bundle is the agent's job:

```bash
python3 tools/metal3_agent_bridge.py apply  --manifest hosts/bmhs.yaml --namespace metal3
python3 tools/metal3_agent_bridge.py verify --expected 2
```

## Actions

| Action | Vendored code |
|---|---|
| `prepare` | render BMHs, then `01_prepare_host.sh` -> `02_configure_host.sh` -> `03_launch_mgmt_cluster.sh` -> `04_verify.sh` |
| `verify` | `04_verify.sh` |
| `clean` | `make clean` |
| `restore` | re-import the pinned tree from its archive, no git |

## Safety and host requirements

The scripts modify host networking, packages, firewall rules and cluster state.
Point them at a host you are willing to reconfigure. `prepare` and `clean`
require `METAL3_LAB_ALLOW_HOST_MUTATION=1`.

Minimum documented size is 4 CPUs and 8 GiB RAM for the Ubuntu 22.04/24.04
management host. KVM is irrelevant: nothing here starts a virtual machine.

The runner records timestamped logs under `$METAL3_LAB_WORK_DIR/artifacts`,
verifies the vendored tree before every action, and logs a warning when the
vendored code contains local edits.

## Editing the vendored code

1. Edit files under `deploy/metal3-lab/vendor/metal3-dev-env/` directly.
2. `python3 tools/vendor_import.py --check` reports edited files as `local change:`
   and confirms the pruned paths are still absent.
3. Record intentional divergences in `vendor/PATCHES.md`.
4. To stop vendoring another upstream path, add it to `PRUNE_PATHS` in
   `tools/vendor_import.py`; to keep one, remove it from that list.
5. `run.sh restore` re-imports the pinned tree, applies the prune list again and
   replaces local edits — re-apply your patches afterwards.

See `vendor/README.md` for the full layout and the import rules.

## What this validates

The runner validates real CAPI/Metal3 control plane deployment on physical
machines: host preparation, BMH registration, introspection and readiness.
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
