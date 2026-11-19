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

## Pre-provision BMC credentials (required before the first BMH)

BMC passwords are intentionally kept out of Cloud Control. Resolve them from the
platform credential store, write an owner-only manifest, and apply the Secrets
to the EPH bootstrap cluster in the same namespace the BMHs use:

~~~bash
python tools/metal3_deploy.py secrets \
  --project <project-id> \
  --namespace metal3 \
  --out-dir /secure/ai-agent-stack/bmc \
  --apply
~~~

`secrets.yaml` is written with mode 0600. Keep it on an encrypted volume, never
commit it, and delete it after the Secrets exist. The EPH Agent fails closed with
`METAL3_BMC_CREDENTIAL_SECRET_MISSING` when a BMH references a Secret that is
missing, so a deployment cannot silently proceed with absent credentials.

Without `--apply`, the command only renders the file; apply it from a host with
the EPH kubeconfig. The same file is produced by `tools/metal3_deploy.py plan`
and is included in the rendered bundle.


## One platform: physical machines

This lab provisions real hardware and never requires libvirt, KVM, a VM domain,
or a second kind/minikube management cluster. The EPH single-node Kubernetes
cluster directly contains the BMH/CAPI resources, EPH Agent, Ironic/BMO,
registry, vbmc, sushy-tools and httpd-infra Pods. Target Agent is likewise a
Kubernetes Deployment in the target cluster. None of these workloads uses
Docker Compose or a host container lifecycle. The upstream VM creation path is
deleted rather than toggled off.

`tools/vendor_import.py` records every refused upstream path in `PRUNE_PATHS`:
libvirt/virtbmc roles and playbooks, host-side emulator containers, the Kamaji PoC, the VM e2e
suite, tilt/developer tooling, and the superseded `04_verify.sh`, cleanup
scripts and `Makefile`. The retained tree contains only the three host-bootstrap
stages the Python lifecycle actually executes:

| Stage | Responsibility |
|---|---|
| `01_prepare_host.sh` | Install host packages, Ansible, kubectl and krew; reuse the EPH single-node cluster |
| `02_configure_host.sh` | Configure real-NIC bridges, firewall and ISO-provided IPA files; start no host containers |
| `03_launch_mgmt_cluster.sh` | Install Ironic/BMO and CAPI/CAPM3 controllers into the existing EPH cluster; stop there |

None of these scripts generates, applies or verifies a BMH, and none invokes
another orchestrator.

## Python owns the pipeline, the agent owns the decisions

| Stage | Code |
|---|---|
| Lifecycle and ordering | `apps/eph_agent/metal3_lab.py`, exposed by `tools/metal3_lab.py` |
| Read the fleet | `tools/metal3_fleet_source.py` reads `AgentMachine` rows and resolves BMC credential references |
| Reason about roles | `apps/eph_agent/fleet_planning.py`: explicit role/pool labels win; otherwise reported CPU, RAM, disk and identity determine the ranking |
| Render BMHs and CAPI | `apps/eph_agent/capi_renderer.py`; when `--data-template` is absent, `data_template.py` derives `Metal3DataTemplate` from the declared physical control-plane interface |
| Apply | `apps/eph_agent/metal3_skill.apply_baremetalhosts`, then `apply_capi_cluster_resources` |
| Configure target networking | `network_stack_adapter.py` applies local plugin manifests idempotently and waits for the declared Deployment/DaemonSet readiness |
| Verify | `observe_baremetalhosts` plus `apps/eph_agent/metal3_readiness.py` for CRDs, controllers, replica sets, registry/BMC/httpd Deployments, host bridges and Ironic authentication |
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

Run the complete host, control-plane and target-CNI pipeline with one command:

```bash
python3 tools/metal3_lab.py auto \
  --project <platform-project-id> \
  --cluster-name target-cluster \
  --control-plane-count 3 \
  --control-plane-endpoint <api-vip> \
  --image-url <eph-live-os-image-url> \
  --network-spec <network.yaml> \
  --deployment-id <cluster-deployment-id> \
  --allow-host-mutation
```

`network.yaml` is the external data source for both host networking and the
plugin stack. No interface or manifest is guessed:

```yaml
control_plane_interface: eno1
registry_endpoint: 10.0.0.10:5000
registry_pause_image: 10.0.0.10:5000/ai-agent-stack/pause:3.10.1
dns_servers: [10.0.0.53]
namespace: kube-system
plugins:
  calico:
    enabled: true
    version: v3.30.2
    config:
      dataplane: eBPF
      kube_proxy_replacement: true
      resource_kind: DaemonSet
      resource_name: calico-node
manifest_files:
  calico: ./calico.yaml
```

The manifest path is relative to `network.yaml`; inline `manifests` is also
supported. Every enabled plugin must declare a local manifest and the workload
used as its readiness evidence. `auto` waits for the CAPI kubeconfig Secret,
writes `target.kubeconfig` with mode `0600`, applies the plugins and waits for
readiness. Calico eBPF is mandatory: the Agent applies FelixConfiguration with
`bpfEnabled`, waits for the Calico workloads, then deletes kube-proxy. Calico
and Tigera image references in containers and initContainers are rewritten to
`registry_endpoint` with their version tags preserved, including
`pod2daemon-flexvol`. Target Kubeadm configurations skip the kube-proxy addon and require containerd,
kubeadm, kubelet and kubectl to be preinstalled in the OS image. When
`--deployment-id` is supplied, it then waits for the platform's
trusted pivot and worker evidence before returning success. Missing input or
pending manifests fail the run.

Prepare only the management host and deploy the target control plane:

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

`prepare` performs these steps in order (`auto` additionally performs step 6):

1. Require explicit host-mutation consent and validate Ubuntu, CPU/RAM and host
   commands before changing anything.
2. Verify the vendored fork against its manifest.
3. Read the platform fleet, reason about roles and render the plan/manifests.
4. Execute `01`, `02` and `03` to create the management environment.
5. Call the agent's apply pipeline directly, with bounded retries.
6. For `auto`: export the CAPI target kubeconfig, apply the declared CNI/data-plane
   manifests and wait for their workload readiness.
7. Call the agent's BMH and management-cluster readiness checks directly.

Read platform-backed pivot/worker evidence instead of trusting artifact presence:

```bash
python3 tools/metal3_deploy.py stages \
  --dir .metal3-lab/hosts \
  --deployment-id <cluster-deployment-id> \
  --require-ready
```

This writes `platform-stages.json` from `ClusterDeployment.plan_result`; local
`network.json` counts only when its status is `ready`.

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
