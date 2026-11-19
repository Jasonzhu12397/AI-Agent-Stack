# Metal3 Lab (physical machines)

The lab is orchestrated by `tools/metal3_lab.py`. There is **no `run.sh`** and no
`make` lifecycle above the agent. Python owns planning, BMH/CAPI rendering,
target deployment, verification, cleanup, retries and evidence.

The host-bootstrap source is vendored under
`deploy/metal3-lab/vendor/metal3-dev-env/`, imported from
`metal3-io/metal3-dev-env` at commit
`7076f07624cdfba990f83bea24f749b37e3baad7`. `VENDOR.json` and
`MANIFEST.sha256` record provenance and the fork's integrity baseline. Runtime
performs no clone, fetch, checkout or submodule operation.

## One platform: physical machines

This lab provisions real hardware behind real BMCs (Redfish/IPMI). There is no
platform switch, virtual machine, virtual BMC or `/dev/kvm` requirement. The
virtual half of upstream dev-env is not disabled; it is deleted.

`tools/vendor_import.py` records every refused upstream path in `PRUNE_PATHS`:
libvirt/virtbmc roles and playbooks, BMC emulators, the Kamaji PoC, the VM e2e
suite, tilt/developer tooling, and the superseded `04_verify.sh`, cleanup
scripts and `Makefile`. The retained tree contains only the three host-bootstrap
stages the Python lifecycle actually executes:

| Stage | Responsibility |
|---|---|
| `01_prepare_host.sh` | Install host packages, Ansible, kind, kubectl and krew |
| `02_configure_host.sh` | Configure real-NIC bridges, firewall, registry and ironic httpd |
| `03_launch_mgmt_cluster.sh` | Start kind, Ironic/BMO and CAPI/CAPM3 controllers; stop there |

None of these scripts generates, applies or verifies a BMH, and none invokes
another orchestrator.

## Python owns the pipeline, the agent owns the decisions

| Stage | Code |
|---|---|
| Lifecycle and ordering | `apps/eph_agent/metal3_lab.py`, exposed by `tools/metal3_lab.py` |
| Read the fleet | `tools/metal3_fleet_source.py` reads `AgentMachine` rows and resolves BMC credential references |
| Reason about roles | `apps/eph_agent/fleet_planning.py`: explicit role/pool labels win; otherwise reported CPU, RAM, disk and identity determine the ranking |
| Render BMHs and CAPI | `apps/eph_agent/capi_renderer.py`, including control-plane and worker-pool ownership |
| Apply | `apps/eph_agent/metal3_skill.apply_baremetalhosts`, then `apply_capi_cluster_resources` |
| Verify | `observe_baremetalhosts` plus `apps/eph_agent/metal3_readiness.py` for CRDs, controllers, replica sets, host bridges, infra containers and Ironic authentication |
| Explain and audit | `plan.json` records every reason and missing fact; `--write-back` mirrors the decision into `AgentMachine` and writes `fleet.machine.role_planned` audit events |

The deployment layers are:

1. The EPH live-OS single-node cluster runs Cluster API and Metal3.
2. The Python lab deploys the target cluster's control-plane masters.
3. The target agent pivots Metal3 data (`execute_capi_pivot`) and provisions the
   worker/data-plane machines (`execute_worker_provisioning`). These require the
   platform's trusted deployment context; the lab never fabricates it.
4. The target network stack installs the CNI and data-plane plugins.

## Commands

Plan without changing the host:

```bash
python3 tools/metal3_lab.py plan \
  --project <platform-project-id> \
  --cluster-name target-cluster \
  --control-plane-count 3 \
  --control-plane-endpoint <api-vip> \
  --image-url <eph-live-os-image-url> \
  --data-template <metal3-data-template.yaml> \
  --write-back
```

Prepare the management host and deploy the target control plane:

```bash
python3 tools/metal3_lab.py prepare \
  --project <platform-project-id> \
  --control-plane-endpoint <api-vip> \
  --image-url <eph-live-os-image-url> \
  --data-template <metal3-data-template.yaml> \
  --allow-host-mutation
```

Inspect the recorded decisions, verify readiness, or plan the target network:

```bash
python3 tools/metal3_deploy.py explain --dir .metal3-lab/hosts
python3 tools/metal3_lab.py verify
python3 tools/metal3_deploy.py stages  --dir .metal3-lab/hosts
python3 tools/metal3_deploy.py network --dir .metal3-lab/hosts --spec network.json
```

Clean the host through explicit argv-only Python stages (no cleanup script and
no `make clean`):

```bash
python3 tools/metal3_lab.py clean --allow-host-mutation
```

## Lifecycle behavior

`prepare` performs these steps in order:

1. Require explicit host-mutation consent and validate Ubuntu, CPU/RAM and host
   commands before changing anything.
2. Verify the vendored fork against its manifest.
3. Read the platform fleet, reason about roles and render the plan/manifests.
4. Execute `01`, `02` and `03` to create the management environment.
5. Call the agent's apply pipeline directly, with bounded retries.
6. Call the agent's BMH and management-cluster readiness checks directly.

Logs are written under `.metal3-lab/artifacts` (or `--work-dir`). Every host
command is an argument vector with `shell=False`; no command string is built for
a shell to interpret.

## Vendored fork workflow

```bash
python3 tools/vendor_import.py --check
python3 tools/metal3_lab.py reference --vendor-source ./metal3-dev-env.tar.gz
python3 tools/metal3_lab.py reseal
```

- `reference` imports upstream into `vendor/metal3-dev-env.upstream` for
  comparison. It never overwrites the running fork.
- After a deliberate edit, record it in `vendor/PATCHES.md` and run `reseal` to
  establish the new manifest baseline. Reseal refuses a tree that reintroduces
  pruned code or loses a required bootstrap file.
- `--check` reports subsequent local drift without treating intentional edits as
  a different upstream commit.

## Safety and host requirements

`prepare` and `clean` require `--allow-host-mutation` (or
`METAL3_LAB_ALLOW_HOST_MUTATION=1`). They install packages and change networking,
firewall rules and cluster state. The documented minimum management host is
Ubuntu 22.04/24.04 with 4 CPUs and 8 GiB RAM. KVM is irrelevant.

## What this validates

The lab validates real CAPI/Metal3 control-plane deployment on physical
machines: host preparation, BMH registration, introspection and readiness. It is
a provider/environment validation tier, **not an AI Agent Stack three-agent
end-to-end test**. It does not by itself prove Cloud Control Agent scheduling,
EPH pivot execution, Target Agent handoff, worker provisioning or EPH cleanup.
Those must be exercised through the Cloud Control API with durable attestations.
Unit and mocked API tests must never be reported as `LIBVIRT_IRONIC` evidence.

## Upstream references

- [Metal3 dev-env](https://github.com/metal3-io/metal3-dev-env)
- [Developer environment guide](https://book.metal3.io/developer_environment/tryit)
- [Metal3 quick-start](https://book.metal3.io/quick-start)
- [BMO E2E guide](https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md)
